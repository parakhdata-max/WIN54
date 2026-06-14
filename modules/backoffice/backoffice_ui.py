"""
Backoffice UI Module
Streamlit UI components for backoffice management

Contains:
- Product info display and editing
- Power editing UI
- Lens parameters editing
- Boxing parameters editing
- Quantity management (CLEAN ARCHITECTURE)
- Allocation window
- Supplier order section
- Document generation (job cards, lab orders, labels)
- Order detail rendering
- Status update modal
- 🔐 Billing safeguards (lock, pricing freeze, debug toggle)

=============================================================================
 CLEAN QUANTITY ARCHITECTURE - SINGLE SOURCE OF TRUTH
=============================================================================

MASTER FIELDS (per line):
    billing_qty       What customer ordered (MASTER)
    allocated_qty     What's from stock
    pending_qty       CALCULATED: billing_qty - allocated_qty
    batch_allocation  Stock breakdown
    manufacturing_route  STOCK / VENDOR / INHOUSE / EXTERNAL_LAB

WORKFLOW:
    User changes quantity
        
    billing_qty updated
        
    Stock state reset (batch_allocation, allocated_qty, batch_status)
        
    refresh_line_state(line) called
        
    Allocation engine runs (clean recomputation)
        
    allocated_qty + pending_qty calculated
        
    Pricing engine runs
        
    billing_total updated

DELETED FIELDS:
     order_qty (replaced by pending_qty calculation)
     final_qty (replaced by billing_qty)
     Manual pending calculations in UI

RULES:
    - UI ONLY edits billing_qty
    - Workflow Engine sets allocation + route
    - Pricing Engine calculates billing_total
    - Allocation Window ONLY edits batch split
    - NO create_allocation_record() calls in UI
    - NO manual route setting in UI

=============================================================================
 🔐 BILLING SAFEGUARDS (Prevents Future Corruption)
=============================================================================

1. BILLING LOCK (Save Validation):
   - Prevents saving orders with total_billing <= 0
   - Catches silent corruption before it hits database
   - Shows clear error message to user

2. PRICING FREEZE (Optional):
   - Sets pricing_locked=True on all lines after save
   - Prevents accidental recomputation after billing finalized
   - Can be checked before running pricing engine

3. DEBUG TOGGLE:
   - Hidden checkbox in Billing tab: "🔍 Debug Pricing"
   - Shows full JSON of each line item
   - Includes: billing_qty, allocated_qty, pending_qty, unit_price,
     billing_total, manufacturing_route, batch_allocation, pricing_locked
   - Helps troubleshoot pricing issues

USAGE:
    # Before saving
    if total_billing <= 0:
        st.error("Billing invalid. Cannot save.")
        return
    
    # After saving (optional)
    for line in all_lines:
        line["pricing_locked"] = True
    
    # In debug mode
    if st.checkbox("Debug Pricing"):
        st.json(line)

=============================================================================
"""

import streamlit as st
import pandas as pd
import datetime
import uuid
import logging
from typing import Dict, List, Optional
from modules.workflow.status import OrderStatus

# Import core dependencies
from modules.sql_adapter import read_product_master
from modules.documents.job_engine import generate_job_card_data
from modules.supplier_orders_management import (
    get_vendor_routed_lines,
    create_supplier_order_from_lines,
    add_supplier_order_button_to_backoffice
)
from .backoffice_logic import refresh_line_state


# Import from other backoffice modules
from .backoffice_helpers import (
    fmt_num,
    fmt_signed,
    get_display_order_id,
    get_display_label,
    power_key,
    sync_power_to_ui,
    force_power_refresh
)
from .backoffice_logic import (
    update_manufacturing_power,
    update_batch_allocation,
    update_line_billing,
    recalculate_order_totals,
    guard_price_mutation,
)
from .backoffice_helpers import load_orders_from_database
from .backoffice_panels import (
    render_power_edit_ui,
    render_lens_params_edit_ui,
    render_boxing_params_edit_ui,
    render_allocation_window,
)

logger = logging.getLogger(__name__)

# ============================================================================
# BACKOFFICE WRITE GUARD — protect line price/discount integrity
# ----------------------------------------------------------------------------
# Why this exists:
#   Backoffice power/product edits were occasionally landing rows with
#   unit_price = 0 (and matching discount=0) in the DB. Validators caught
#   it on next open, but the data was already corrupted. The right design
#   is to enforce at the WRITE BOUNDARY, not the read boundary: refuse to
#   commit a save that would leave a line unbillable.
#
# Behaviour (Strict-with-mirror + loud-fail):
#   • Refuses any save where the resulting unit_price <= 0.
#   • AUTO-RECOVERY for the common two-eye case: if another non-deleted
#     line in the SAME order has the same product_id with a non-zero
#     unit_price, mirror that price (and gst_percent) and ALLOW the save.
#     Returns the mirrored values so the caller writes them too. This
#     does not invent a price — it only copies a price that already
#     exists on this order.
#   • DISCOUNT CONSISTENCY: a line with a discount rule attached but
#     resulting amount = 0 (or vice versa) is rejected — that is the
#     "discount was dropped by the edit" symptom.
#   • Failure is LOUD: shows a clear st.error so the user sees exactly
#     what was blocked and why, and logs to the python logger for ops.
#
# Returns (ok, msg, mirrored_price_or_none, mirrored_gst_or_none).
# Callers must update their UPDATE params with the mirrored values when
# they are not None.
# ============================================================================
def _guard_line_price_before_write(
    line: Dict,
    order: Dict,
    all_lines: Optional[list] = None,
    context: str = "backoffice-write",
):
    """Strict-with-mirror price + discount-consistency gate.

    See module comment above for full semantics. Pure-function: does not
    write to DB. Caller decides what to do with (ok=False, ...): typically
    show the error and skip the UPDATE.
    """
    import logging
    _log = logging.getLogger(__name__)

    try:
        _qty = float(line.get("quantity") or line.get("qty") or 0)
        _up  = float(line.get("unit_price") or 0)
        _pid = str(line.get("product_id") or "")
        _disc_amt = float(line.get("discount_amount") or 0)
        _disc_rule = str(line.get("discount_rule") or "").strip()
        _name = (
            line.get("product_name")
            or (line.get("product") or {}).get("product_name")
            or "(line)"
        )
        _eye = str(line.get("eye_side") or line.get("eye") or "").upper()

        # --- 1. Price > 0 (with mirror auto-recovery) -----------------------
        _mirrored_up = None
        _mirrored_gst = None
        if _up <= 0 and _qty > 0:
            # Try to mirror from the other eye / sibling line with same product.
            _siblings = all_lines or order.get("lines") or []
            _src = None
            for _s in _siblings:
                if _s is line:
                    continue
                if bool(_s.get("is_deleted")):
                    continue
                if str(_s.get("product_id") or "") != _pid or not _pid:
                    continue
                _s_up = float(_s.get("unit_price") or 0)
                if _s_up > 0:
                    _src = _s
                    break
            if _src is not None:
                _mirrored_up  = float(_src.get("unit_price") or 0)
                _mirrored_gst = float(_src.get("gst_percent") or line.get("gst_percent") or 0)
                _log.warning(
                    "[%s] price guard auto-mirrored zero unit_price on %s/%s "
                    "from sibling line (product_id=%s): %.2f",
                    context, _name, _eye, _pid, _mirrored_up,
                )
                # mirror into the in-memory line so the rest of the save uses it
                line["unit_price"]  = _mirrored_up
                if _mirrored_gst:
                    line["gst_percent"] = _mirrored_gst
                _up = _mirrored_up
            else:
                _msg = (
                    f"⛔ Save blocked: {_name} ({_eye or 'line'}) has qty {int(_qty)} "
                    f"but unit_price = 0, and no sibling line with the same "
                    f"product has a price to mirror. Set a price first."
                )
                _log.warning("[%s] price guard BLOCK: %s", context, _msg)
                try:
                    st.error(_msg)
                except Exception:
                    pass
                return False, _msg, None, None

        # --- 2. Discount consistency ----------------------------------------
        # Rule attached but amount 0, or amount > 0 but no rule -> the edit
        # has decoupled the two; that is exactly the symptom of a dropped
        # discount during product/power change. Block it.
        if _disc_rule and _disc_amt <= 0 and _up > 0 and _qty > 0:
            _msg = (
                f"⛔ Save blocked: {_name} ({_eye or 'line'}) carries a "
                f"discount rule ({_disc_rule}) but the discount amount is 0. "
                f"Re-apply the discount or clear the rule before saving."
            )
            _log.warning("[%s] discount guard BLOCK: %s", context, _msg)
            try:
                st.error(_msg)
            except Exception:
                pass
            return False, _msg, _mirrored_up, _mirrored_gst

        # --- 3. Over-discount: discount cannot exceed line gross (F7) -------
        # _bo_line_amounts uses max(gross - discount, 0) so billing_total never
        # goes negative, but discount_amount in the DB would remain incorrect,
        # producing wrong GST and wrong register entries.
        if _disc_amt > 0 and _up > 0 and _qty > 0:
            _gross = round(_up * _qty, 2)
            if _disc_amt > _gross:
                _msg = (
                    f"⛔ Save blocked: {_name} ({_eye or 'line'}) has a discount "
                    f"of ₹{_disc_amt:.2f} which exceeds the line gross of ₹{_gross:.2f}. "
                    f"Reduce the discount before saving."
                )
                _log.warning("[%s] over-discount BLOCK: %s", context, _msg)
                try:
                    st.error(_msg)
                except Exception:
                    pass
                return False, _msg, _mirrored_up, _mirrored_gst

        return True, "", _mirrored_up, _mirrored_gst

    except Exception as _ge:
        # Guard ITSELF failed — fail CLOSED (refuse the save), do not swallow
        # silently. Same principle as the section_guard fix earlier.
        import logging, traceback
        logging.getLogger(__name__).error(
            "[%s] price guard internal error - BLOCKING save: %s\n%s",
            context, _ge, traceback.format_exc(),
        )
        try:
            st.error(
                "⛔ Save blocked — price/discount guard failed unexpectedly. "
                "See server log. (This is a safety stop to prevent corrupt data.)"
            )
        except Exception:
            pass
        return False, str(_ge), None, None


def _service_route_for_group(service_group: str, direct: bool = False) -> str:
    """Line-level production route for service-only order lines."""
    group = str(service_group or "").upper().strip()
    if direct:
        return "SERVICE"
    if group == "COLOURING":
        return "INHOUSE"
    if group == "FITTING":
        return "FITTING"
    return "SERVICE"


def _ensure_bo_service_product(service_group: str, label: str, gst_percent: float = 0.0) -> Optional[str]:
    """Create/reuse a lightweight product row so service lines print cleanly everywhere."""
    name = str(label or "").strip() or f"{str(service_group or 'Service').title()} Service"
    group = str(service_group or "SERVICE").upper().strip()
    try:
        from modules.sql_adapter import run_query, run_write
        rows = run_query(
            "SELECT id::text AS id FROM products WHERE LOWER(product_name)=LOWER(%s) LIMIT 1",
            (name,),
        ) or []
        if rows:
            return str(rows[0]["id"])
        pid = str(uuid.uuid4())
        run_write(
            """
            INSERT INTO products
                (id, product_name, brand, main_group, category, unit,
                 gst_percent, is_active, created_at)
            VALUES
                (%(id)s::uuid, %(pn)s, 'Services', 'Services', %(cat)s, 'SERVICE',
                 %(gst)s, TRUE, NOW())
            ON CONFLICT DO NOTHING
            """,
            {"id": pid, "pn": name, "cat": group.title(), "gst": float(gst_percent or 0)},
        )
        return pid
    except Exception:
        return None


def _bo_service_line_amounts(amount: float, gst_percent: float, order_type: str) -> tuple[float, float]:
    """Return (total, gst_amount) using the same retail/wholesale tax convention as products."""
    amt = round(float(amount or 0), 2)
    gst = float(gst_percent or 0)
    if gst <= 0:
        return amt, 0.0
    if str(order_type or "").upper() == "RETAIL":
        return amt, round(amt - (amt / (1 + gst / 100)), 2)
    gst_amt = round(amt * gst / 100, 2)
    return round(amt + gst_amt, 2), gst_amt


def _bo_complete_service_rows(service_rows: list) -> list:
    """Return Service Master rows plus mandatory service fallbacks.

    This prevents collectable leakage when a service exists in business rules
    but is missing/inactive in service_master. Configured Service Master rows
    still win; fallback rows only fill gaps so Backoffice always offers the
    same charge families as punching.
    """
    try:
        from modules.core.business_rules import SERVICE_CHARGE_TYPES
    except Exception:
        SERVICE_CHARGE_TYPES = {}
    rows = [dict(r) for r in (service_rows or [])]
    seen_groups = {str(r.get("service_group") or "").upper().strip() for r in rows}
    for group in ("FITTING", "COLOURING", "COURIER", "CONSULTATION", "EYE_TESTING", "MISC"):
        if group in seen_groups:
            continue
        cfg = SERVICE_CHARGE_TYPES.get(group, SERVICE_CHARGE_TYPES.get("MISC", {}))
        rows.append({
            "service_code": group,
            "service_group": group,
            "service_name": cfg.get("label") or group.title(),
            "gst_percent": float(cfg.get("default_gst") or 0),
            "default_price": 0.0,
            "production_route": group if group in ("FITTING", "COLOURING") else "",
            "_fallback": True,
        })
        seen_groups.add(group)
    return rows


def _bo_line_lens_params(line: Dict) -> Dict:
    lp = (line or {}).get("lens_params") or {}
    if isinstance(lp, str):
        try:
            import json as _json_lp
            lp = _json_lp.loads(lp)
        except Exception:
            lp = {}
    return lp if isinstance(lp, dict) else {}


def _bo_service_family_for_line(line: Dict) -> str:
    lp = _bo_line_lens_params(line)
    text = " ".join(str(x or "") for x in (
        lp.get("charge_type"),
        lp.get("service_type"),
        lp.get("service_group"),
        lp.get("service_production_type"),
        lp.get("service_code"),
        lp.get("service_description"),
        line.get("product_name") if line else "",
    )).upper()
    if "COLOUR" in text or "COLOR" in text or "TINT" in text:
        return "COLOURING"
    if "FITTING" in text or "FIT_" in text:
        return "FITTING"
    if "COURIER" in text or "DELIVERY" in text or "FREIGHT" in text:
        return "COURIER"
    if "CONSULT" in text:
        return "CONSULTATION"
    if "TEST" in text or "REFRACTION" in text:
        return "EYE_TESTING"
    if bool(line.get("is_service_line")) or str(line.get("eye_side") or "").upper() in ("S", "SERVICE"):
        return "MISC"
    return ""


def _bo_suspected_missing_services(lines: list) -> list:
    present = {_bo_service_family_for_line(l) for l in (lines or []) if _bo_service_family_for_line(l)}
    suspects = []
    for line in (lines or []):
        if bool(line.get("is_service_line")):
            continue
        lp = _bo_line_lens_params(line)
        colour_values = [
            lp.get("colouring_required"),
            lp.get("coloring_required"),
            lp.get("tint_required"),
            lp.get("tinted"),
            lp.get("colour"),
            lp.get("color"),
            lp.get("colour_mix"),
            lp.get("tint"),
            lp.get("treatment"),
        ]
        colour_truthy = any(
            str(v or "").strip().upper() not in ("", "NO", "N", "FALSE", "0", "NONE", "CLEAR", "TRANSPARENT", "REGULAR")
            for v in colour_values
        )
        colour_route = str(lp.get("service_production_type") or lp.get("manufacturing_route") or "").upper() in ("COLOURING", "COLOURING_HC")
        if colour_truthy or colour_route:
            if "COLOURING" not in present:
                suspects.append("COLOURING")

        fitting_required = str(lp.get("fitting_required") or "").strip().upper() in ("1", "Y", "YES", "TRUE", "REQUIRED")
        fitting_height = str(lp.get("fitting_height") or lp.get("fit_height") or "").strip()
        fitting_route = str(lp.get("service_production_type") or lp.get("manufacturing_route") or "").upper() == "FITTING"
        if fitting_required or fitting_route or bool(fitting_height):
            if "FITTING" not in present:
                suspects.append("FITTING")
    ordered = ["COLOURING", "FITTING", "COURIER", "CONSULTATION", "EYE_TESTING", "MISC"]
    return [s for s in ordered if s in set(suspects)]


def _bo_line_display_name(line: Dict) -> str:
    """Display name for product and service rows, including old cached rows."""
    import json as _name_json
    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _name_json.loads(lp)
        except Exception:
            lp = {}
    name = str(line.get("product_name") or "").strip()
    if not name or name.lower() in ("unknown product", "unknown", "none", "null"):
        name = str(
            lp.get("service_display_name")
            or lp.get("display_product_name")
            or lp.get("service_description")
            or lp.get("description")
            or ""
        ).strip()
    return name or "Service"


def _bo_float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


def _bo_int(value, default: int = 0) -> int:
    try:
        return int(float(value if value is not None else default))
    except Exception:
        return int(default)


def _bo_release_stock_allocation_for_line(line: Dict, reason: str = "backoffice_edit") -> None:
    """Release existing inventory reservation before a product/qty reset.

    Physical stock remains untouched here. This only frees allocated_qty so
    the old batch becomes available again when staff changes the line.
    """
    try:
        from modules.sql_adapter import run_write as _rw_rel_stock
        import json as _rel_json

        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _rel_json.loads(lp)
            except Exception:
                lp = {}
        if not isinstance(lp, dict):
            lp = {}

        route = str(line.get("manufacturing_route") or lp.get("manufacturing_route") or "").upper()
        alloc_qty_total = _bo_int(line.get("allocated_qty") or 0)
        alloc_rows = line.get("batch_allocation") or lp.get("batch_allocation") or []
        if isinstance(alloc_rows, dict):
            alloc_rows = [alloc_rows]
        if not alloc_rows and alloc_qty_total > 0:
            stock_id = str(lp.get("stock_id") or lp.get("batch_id") or "").strip()
            batch_no = str(lp.get("batch_no") or line.get("batch_no") or "").strip()
            alloc_rows = [{
                "stock_id": stock_id,
                "batch_id": stock_id,
                "batch_no": batch_no,
                "allocated_qty": alloc_qty_total,
            }]

        if route != "STOCK" and not alloc_rows:
            return

        pid = str(line.get("product_id") or "").strip()
        for alloc in alloc_rows:
            if not isinstance(alloc, dict):
                continue
            qty = _bo_int(alloc.get("allocated_qty") or alloc.get("qty") or 0)
            if qty <= 0:
                continue
            sid = str(alloc.get("stock_id") or alloc.get("batch_id") or "").strip()
            bno = str(alloc.get("batch_no") or "").strip()
            if sid:
                _rw_rel_stock(
                    """
                    UPDATE inventory_stock
                       SET allocated_qty = GREATEST(0, COALESCE(allocated_qty,0) - %(qty)s),
                           updated_at = NOW()
                     WHERE id = %(sid)s::uuid
                    """,
                    {"qty": qty, "sid": sid},
                )
            elif pid and bno:
                _rw_rel_stock(
                    """
                    UPDATE inventory_stock
                       SET allocated_qty = GREATEST(0, COALESCE(allocated_qty,0) - %(qty)s),
                           updated_at = NOW()
                     WHERE product_id = %(pid)s::uuid
                       AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%(bno)s))
                    """,
                    {"qty": qty, "pid": pid, "bno": bno},
                )
    except Exception as _rel_err:
        logger.warning("[allocation_release] %s failed: %s", reason, _rel_err)


def _bo_is_service_line(line: Dict) -> bool:
    return (
        bool(line.get("is_service_line"))
        or str(line.get("eye_side") or "").upper() in ("S", "SERVICE")
        or str(line.get("unit") or "").upper() == "SERVICE"
    )


def _bo_qty_display(line: Dict) -> str:
    """Human quantity label for product rows and pair-based service rows."""
    if _bo_is_service_line(line):
        lp = _bo_line_lens_params(line)
        factor = lp.get("service_qty_factor")
        try:
            factor = float(factor)
        except Exception:
            factor = 0.0
        if factor > 0:
            if abs(factor - 0.5) < 0.001:
                return "0.5 pair"
            if abs(factor - 1.0) < 0.001:
                return "1 pair"
            return f"{factor:g} pair"
        return "1 service"

    qty = int(line.get("billing_qty") or line.get("quantity") or 0)
    box_size = int(line.get("box_size") or 1)
    unit = str(line.get("unit") or "PCS").upper()
    if unit == "BOX" and box_size > 1:
        boxes = qty // box_size
        pcs_rem = qty % box_size
        return f"{boxes}B" + (f"+{pcs_rem}P" if pcs_rem else "") + f" ({qty}pcs)"
    return f"{qty} PCS"


def _bo_line_amounts(line: Dict, order_type: str = "WHOLESALE") -> Dict[str, float]:
    """Return canonical taxable/GST/grand for a line.

    order_lines historically stores lens billing_total as taxable/net, while
    service billing_total can be GST-inclusive. UI summaries must not mix
    those meanings, so derive from unit_price/qty/discount and stored GST.
    """
    qty = _bo_int(line.get("billing_qty") or line.get("quantity") or 1, 1)
    unit_price = _bo_float(line.get("unit_price"))
    discount = _bo_float(line.get("discount_amount"))
    gst_pct = _bo_float(line.get("gst_percent_used") or line.get("gst_percent"))
    stored_gst = _bo_float(line.get("gst_amount"))
    order_type = str(order_type or "WHOLESALE").upper()

    gross = round(unit_price * qty, 2)
    taxable = round(max(gross - discount, 0), 2)

    if order_type == "RETAIL":
        grand = taxable
        gst = stored_gst
        if gst <= 0 and gst_pct > 0 and taxable > 0:
            gst = round(taxable - (taxable / (1 + gst_pct / 100)), 2)
            taxable = round(grand - gst, 2)
        return {"taxable": taxable, "gst": gst, "grand": grand}

    gst = stored_gst
    if gst <= 0 and gst_pct > 0 and taxable > 0:
        gst = round(taxable * gst_pct / 100, 2)
    grand = round(taxable + gst, 2)
    return {"taxable": taxable, "gst": gst, "grand": grand}


def _bo_normalize_line_numbers(line: Dict) -> Dict:
    """Convert DB Decimal values to plain Python numbers before UI math."""
    for key in (
        "unit_price", "billing_total", "total_price", "gst_percent",
        "gst_amount", "discount_amount", "discount_percent"
    ):
        if key in line:
            line[key] = _bo_float(line.get(key))
    for key in ("billing_qty", "quantity", "allocated_qty", "ready_qty", "billed_qty", "box_size"):
        if key in line:
            line[key] = _bo_int(line.get(key), 1 if key in ("billing_qty", "quantity", "box_size") else 0)
    return line


def _bo_enforce_wholesale_price(line: Dict, order_type: str) -> Dict:
    """Final UI guard: wholesale product lines must display DB selling_price.

    This intentionally runs even on stale session-state line dicts. It can
    resolve by product_id, and if a stale line lost product_id it falls back to
    product_name. Service/manual-price lines are left untouched.
    """
    try:
        _ot_str = str(order_type or "").upper().strip()
        # Explicit RETAIL/empty guard — never override MRP-based retail prices
        if _ot_str != "WHOLESALE" or bool(line.get("is_service_line")):
            return line
        # Extra safety: if the line itself was stamped as RETAIL, skip it
        if str(line.get("order_type") or "").upper() == "RETAIL":
            return line
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _json_wsp
            try:
                lp = _json_wsp.loads(lp)
            except Exception:
                lp = {}
        if (
            "manual" in str(line.get("price_source") or "").lower()
            or bool(line.get("manual_price_override"))
            or bool((lp or {}).get("manual_price_override"))
            or bool((lp or {}).get("price_locked"))
        ):
            return line

        pid = str(line.get("product_id") or "").strip()
        if not pid:
            pname = str(line.get("product_name") or "").strip()
            if pname:
                from modules.sql_adapter import run_query as _rq_wsp
                rows = _rq_wsp(
                    """
                    SELECT id::text AS id, COALESCE(unit,'PCS') AS unit,
                           GREATEST(COALESCE(box_size,1),1) AS box_size,
                           COALESCE(gst_percent,0) AS gst_percent
                    FROM products
                    WHERE LOWER(product_name) = LOWER(%s)
                       OR LOWER(TRIM(product_name)) = LOWER(TRIM(%s))
                    ORDER BY COALESCE(is_active, TRUE) DESC, product_name
                    LIMIT 1
                    """,
                    (pname, pname),
                ) or []
                if rows:
                    pid = str(rows[0].get("id") or "")
                    line["product_id"] = pid
                    line["unit"] = line.get("unit") or rows[0].get("unit") or "PCS"
                    line["box_size"] = int(float(line.get("box_size") or rows[0].get("box_size") or 1))
                    if not float(line.get("gst_percent") or 0):
                        line["gst_percent"] = float(rows[0].get("gst_percent") or 0)
        if not pid:
            return line

        from modules.core.price_source_resolver import resolve_db_price
        from modules.core.price_qty_governor import compute_line_gst

        resolved = resolve_db_price(pid, "WHOLESALE", product=line, prefer_batch=True)
        pcs = float(resolved.get("pcs_price") or 0)
        if pcs <= 0:
            return line
        qty = int(line.get("billing_qty") or line.get("quantity") or 0)
        cur = float(line.get("unit_price") or 0)
        if qty <= 0 or abs(cur - pcs) <= 0.5:
            line["unit_price"] = pcs
            line["price_source"] = str(resolved.get("source") or line.get("price_source") or "")
            line["tax_inclusive"] = False
            return line

        disc_pc = float(line.get("discount_percent") or 0)
        gross = round(pcs * qty, 2)
        disc = round(gross * disc_pc / 100, 2) if disc_pc > 0 else float(line.get("discount_amount") or 0)
        net = round(max(0.0, gross - disc), 2)
        gst = compute_line_gst(
            net / max(qty, 1),
            qty,
            float(line.get("gst_percent") or 0),
            "WHOLESALE",
        )
        line["unit_price"] = pcs
        line["billing_total"] = gst["subtotal"]
        line["total_price"] = gst["subtotal"]
        line["gst_amount"] = gst["gst_amount"]
        line["discount_amount"] = disc
        line["price_source"] = str(resolved.get("source") or "BATCH")
        line["tax_inclusive"] = False
    except Exception:
        pass
    return line

# Assignment panel — supplier / job-card allocation before save
try:
    from .assignment_panel import (
        render_assignment_panel,
        init_assignment_state,
    )
    _ASSIGNMENT_PANEL_AVAILABLE = True
except ImportError:
    _ASSIGNMENT_PANEL_AVAILABLE = False

# Import sidebar component
try:
    from .backoffice_sidebar import render_backoffice_sidebar
except ImportError:
    # Sidebar is optional - if not available, just skip it
    render_backoffice_sidebar = None


# ==========================================================
# SESSION STATE INITIALISATION
# ==========================================================

def init_backoffice_state():
    """
    Initialise all bo_ session state keys used by backoffice_ui.
    Safe to call on every render — only sets keys that don't exist yet.
    """
    defaults = {
        "bo_view_mode":              "dashboard",
        "bo_selected_order_id":      None,
        "bo_active_orders":          [],
        "bo_orders_loaded":          False,
        "bo_editing_line":           None,
        "bo_show_allocation_window": False,
        "bo_allocation_line_idx":    None,
        "bo_product_change_modal":   {"active": False},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # Assignment panel state
    if _ASSIGNMENT_PANEL_AVAILABLE:
        init_assignment_state()

def render_product_info_display(line: Dict, idx: int, eye_label: str, order: Dict):
    """
    Display product and brand information with edit capability
    
    Args:
        line: Line item dictionary
        idx: Line index  
        eye_label: 'R' or 'L' for right/left eye
        order: Parent order dict
    """
    st.markdown("####  Product Information")
    
    # Display mode - always show product info
    col_info, col_edit = st.columns([3, 1])
    
    with col_info:
        product_name = line.get('product_name', 'N/A')
        brand = line.get('brand', 'N/A')
        main_group = line.get('main_group', 'N/A')
        unit = line.get('unit', 'N/A')
        
        st.info(
            f"**Product:** {product_name}\n\n"
            f"**Brand:** {brand}\n\n"
            f"**Category:** {main_group}\n\n"
            f"**Unit:** {unit}"
        )
    
    with col_edit:
        if st.button(" Change", key=f"change_product_{eye_label}_{idx}", use_container_width=True):
            #  FIX: Set modal state instead of inline edit
            st.session_state['bo_product_change_modal'] = {
                'active': True,
                'line': line,
                'idx': idx,
                'eye_label': eye_label,
                'order': order
            }
            st.rerun()


def render_product_sync_option(group: Dict, product_id: str):
    """
    Render option to sync product change across both eyes
    
    Args:
        group: Product group containing R and L lines
        product_id: Current product ID
    """
    if group['R'] and group['L']:
        st.markdown("---")
        col_icon, col_label = st.columns([1, 5])
        
        with col_icon:
            st.markdown("")
        with col_label:
            sync_enabled = st.checkbox(
                "Apply product changes to both eyes simultaneously",
                value=st.session_state.get(f'sync_product_{product_id}', False),
                key=f'sync_product_{product_id}',
                help="When enabled, changing the product on one eye will automatically update both R and L"
            )
            
            if sync_enabled:
                st.caption(" Both R and L eyes will use the same product")



def _bo_repricing_for_product(new_pid: str, line: Dict, order: Dict):
    """Resolve unit_price + gst_percent for a product change in backoffice.

    Uses the central pricing hierarchy first so ophthalmic RX edits respect
    the selected index/coating/treatment. Falls back to inventory prices.
    Returns (unit_price_per_piece, gst_percent).
    """
    try:
        from modules.sql_adapter import run_query as _rq_re
        if not new_pid:
            return 0.0, 0.0
        _ot = str(order.get("order_type") or "RETAIL").upper()
        _lp = line.get("lens_params") or {}
        if not isinstance(_lp, dict):
            _lp = {}
        _index_value = (
            _lp.get("lens_index")
            or _lp.get("index")
            or _lp.get("Lens Index")
            or _lp.get("index_value")
        )
        _coating = _lp.get("coating") or _lp.get("LensCoating") or _lp.get("lens_coating")
        _treatment = (
            _lp.get("treatment")
            or _lp.get("material")
            or _lp.get("Material / Treatment")
            or "Clear"
        )

        try:
            from modules.core.price_source_resolver import resolve_db_price as _resolve_db_price

            _resolved = _resolve_db_price(
                new_pid,
                _ot,
                product=line,
                prefer_batch=False,
                index_value=_index_value,
                coating=_coating,
                treatment=_treatment,
            )
            _pcs_price = float((_resolved or {}).get("pcs_price") or 0)
            if (_resolved or {}).get("found") and _pcs_price > 0:
                _gst = float((_resolved or {}).get("gst_percent") or 0)
                return _pcs_price, _gst
        except Exception as _resolver_err:
            logger.warning(
                "Backoffice product-change spec price resolver failed for product %s: %s",
                new_pid,
                _resolver_err,
                exc_info=True,
            )

        _rows = _rq_re("""
            SELECT
                COALESCE(p.gst_percent, 0)         AS gst_percent,
                COALESCE(MAX(i.selling_price), 0)  AS selling_price,
                COALESCE(MAX(i.mrp), 0)            AS mrp
            FROM products p
            LEFT JOIN inventory_stock i
                   ON i.product_id = p.id
                  AND COALESCE(i.is_active, TRUE) = TRUE
            WHERE p.id = %s::uuid
            GROUP BY p.gst_percent
            LIMIT 1
        """, (new_pid,)) or []
        if not _rows:
            return 0.0, 0.0
        _r = _rows[0]
        _gst = float(_r.get("gst_percent") or 0)
        _sp  = float(_r.get("selling_price") or 0)
        _mrp = float(_r.get("mrp") or 0)
        _price = (_mrp or _sp) if _ot == "RETAIL" else (_sp or _mrp)
        try:
            from modules.core.price_qty_governor import normalize_to_pcs_price
            if _price > 0:
                _price = normalize_to_pcs_price(_price, line)
        except Exception:
            pass
        return float(_price or 0), _gst
    except Exception as _repricing_err:
        logger.warning(
            "Backoffice product-change repricing failed for product %s: %s",
            new_pid,
            _repricing_err,
            exc_info=True,
        )
        return 0.0, 0.0


def _bo_refresh_order_total_value(order_id: str) -> None:
    """Recompute orders.total_value from active order_lines after product/pricing edits."""
    try:
        from modules.sql_adapter import run_write as _rw_h, run_query as _rq_h
        from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
        from decimal import Decimal, ROUND_HALF_UP
        order_id = _resolve_order_uuid(order_id) or ""
        if not order_id or len(str(order_id)) < 10:
            return
        _rows = _rq_h("""
            SELECT COALESCE(SUM(COALESCE(ol.billing_total, ol.total_price, 0)), 0) AS net_total,
                   COALESCE(MAX(o.order_type), 'RETAIL') AS order_type
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, {"oid": order_id}) or []
        if _rows:
            _net = float(_rows[0].get("net_total") or 0)
            if str(_rows[0].get("order_type") or "").upper() == "RETAIL":
                _net = float(Decimal(str(_net)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            _rw_h(
                "UPDATE orders SET total_value=%(tv)s, updated_at=NOW() WHERE id=%(oid)s::uuid",
                {"tv": round(_net, 2), "oid": order_id},
            )
    except Exception:
        pass

@st.dialog(" Change Product", width="large")
def product_change_dialog():
    """
    Product change dialog — aligned with punching system.
    Shows: Category → Brand → Product → Index → Coating → Treatment
    For frames: Brand → Product (model number)
    Writes change to DB immediately.
    """
    import json as _pcd_json
    modal_state = st.session_state.get("bo_product_change_modal", {})
    if not modal_state.get("active", False):
        return
    st.markdown(
        """
<style>
div[role="dialog"] {
    max-height: 94vh !important;
}
div[role="dialog"] > div {
    max-height: 90vh !important;
    overflow-y: auto !important;
    padding-bottom: 1rem !important;
}
div[role="dialog"] [data-testid="stHorizontalBlock"] {
    gap: 0.55rem !important;
}
div[role="dialog"] [data-testid="stVerticalBlock"] {
    gap: 0.35rem !important;
}
div[role="dialog"] [data-baseweb="popover"] [role="listbox"],
div[role="dialog"] [data-baseweb="menu"],
div[role="dialog"] ul[role="listbox"] {
    max-height: 46vh !important;
    overflow-y: auto !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )

    line      = modal_state["line"]
    idx       = modal_state["idx"]
    eye_label = modal_state["eye_label"]
    order     = modal_state["order"]
    order_id  = str(order.get("id") or order.get("order_id") or "")
    line_id   = str(line.get("line_id") or line.get("id") or "")

    st.warning(f"Changing product for **{eye_label} Eye** — Line #{idx + 1}")
    st.caption("Current: " + line.get("product_name","N/A") + " | " + line.get("brand","N/A"))

    # ── Load product master ───────────────────────────────────────────
    try:
        from modules.sql_adapter import read_product_master
        products_df = read_product_master()
    except Exception as _e:
        st.error(f"Could not load products: {_e}"); return
    if products_df is None or products_df.empty:
        st.error("No products available"); return

    # ── Is current line a frame? ──────────────────────────────────────
    _cur_mg = str(line.get("main_group") or "").lower()
    _is_frame = "frame" in _cur_mg or "sunglass" in _cur_mg


    # ── Central product selector (same as punching) ───────────────────
    st.markdown("#### Select New Product")
    _pcd_selector_result = None
    try:
        from modules.ui_product_selector import render_product_selector as _render_pcd_selector
        st.session_state["_current_order_type"] = str(order.get("order_type") or "RETAIL").upper()
        _pcd_selector_result = _render_pcd_selector()
    except Exception as _sel_err:
        st.caption(f"Central product selector unavailable, using fallback selector: {_sel_err}")

    _using_central_selector = bool(_pcd_selector_result and _pcd_selector_result.get("product_row"))
    if _using_central_selector:
        _product_row_dict = dict(_pcd_selector_result.get("product_row") or {})
        _prod_row = pd.Series({
            "product_id": _product_row_dict.get("product_id"),
            "product_name": _product_row_dict.get("product_name"),
            "brand": _product_row_dict.get("brand", ""),
            "main_group": _product_row_dict.get("main_group", ""),
            "material": _product_row_dict.get("material", ""),
            "index_value": _product_row_dict.get("lens_index") or _product_row_dict.get("index_value") or "",
            "coating_type": _product_row_dict.get("coating_type") or _product_row_dict.get("coating") or "",
            "gst_percent": _product_row_dict.get("gst_percent", 0),
            "mrp": _product_row_dict.get("mrp", 0),
            "selling_price": _product_row_dict.get("selling_price", 0),
            "purchase_rate": _product_row_dict.get("purchase_rate", 0),
            "unit": _product_row_dict.get("unit", "PCS"),
            "box_size": _product_row_dict.get("box_size", 1),
            "batch_no": _product_row_dict.get("batch_no", ""),
            "available_qty": _product_row_dict.get("available_qty", 0),
        })
        _sel_group = str(_prod_row.get("main_group") or "")
        _is_frame_sel = bool(_pcd_selector_result.get("is_frame")) or "frame" in _sel_group.lower() or "sunglass" in _sel_group.lower()
    else:
        st.caption("Use the selector above, or choose from fallback Category / Brand / Product below.")
        # ── Fallback Category/Brand/Product filter ─────────────────────
        _groups = [""] + sorted(products_df["main_group"].dropna().astype(str).unique())
        _default_g = str(line.get("main_group") or "")
        _g_idx = _groups.index(_default_g) if _default_g in _groups else 0
        _sel_group = st.selectbox("Category", _groups, index=_g_idx, key="pcd_group")
        _is_frame_sel = "frame" in _sel_group.lower() or "sunglass" in _sel_group.lower()

        _pf = products_df.copy()
        if _sel_group:
            _pf = _pf[_pf["main_group"].astype(str) == _sel_group]
        _brands = [""] + sorted(_pf["brand"].dropna().astype(str).unique())
        _def_b = str(line.get("brand") or "")
        _b_idx = _brands.index(_def_b) if _def_b in _brands else 0
        _sel_brand = st.selectbox("Brand", _brands, index=_b_idx, key="pcd_brand")
        if _sel_brand:
            _pf = _pf[_pf["brand"].astype(str) == _sel_brand]

        if _is_frame_sel:
            _frame_name_search = st.text_input(
                "Search frame by name",
                value="",
                placeholder="Type frame model/name...",
                key="pcd_frame_name_search",
            ).strip()
            if _frame_name_search:
                _pf = _pf[
                    _pf["product_name"].astype(str).str.contains(
                        _frame_name_search, case=False, na=False, regex=False
                    )
                ]
            if _pf.empty:
                _pf = products_df[
                    products_df["main_group"].astype(str).str.lower().str.contains(
                        "frame|sunglass", regex=True, na=False
                    )
                ].copy()
                if _sel_brand:
                    _pf = _pf[_pf["brand"].astype(str) == _sel_brand]
                st.caption("Frame list reloaded from frame master.")
        _prod_list = [""] + sorted(_pf["product_name"].dropna().astype(str).unique())
        _def_p = str(line.get("product_name",""))
        _p_idx = _prod_list.index(_def_p) if _def_p in _prod_list else 0
        _prod_label = "Frame Model" if _is_frame_sel else "Select Product *"
        _sel_prod = st.selectbox(_prod_label, _prod_list, index=_p_idx, key="pcd_prod")

        if not _sel_prod:
            _pc1, _pc2 = st.columns(2)
            if _pc1.button("Cancel", key="pcd_cancel_nosel"):
                st.session_state["bo_product_change_modal"] = {"active": False}
                st.rerun()
            return

        _prod_row = _pf[_pf["product_name"].astype(str) == _sel_prod]
        if _prod_row.empty:
            st.warning("Product not found"); return
        _prod_row = _prod_row.iloc[0]

    # ── Lens parameters (Index / Coating / Treatment) — not shown for frames ──
    _new_index    = ""
    _new_coating  = ""
    _new_treatment= ""

    if not _is_frame_sel:
        _lp_cur = line.get("lens_params") or {}
        if isinstance(_lp_cur, str):
            try: _lp_cur = _pcd_json.loads(_lp_cur)
            except Exception as _e:
                logger.warning("Suppressed error: %s", _e)
                _lp_cur = {}

        _li1, _li2, _li3 = st.columns(3)
        _sel_index_default = (
            _prod_row.get("index_value")
            if _using_central_selector and str(_prod_row.get("index_value") or "").strip()
            else (_lp_cur.get("lens_index") or _lp_cur.get("index") or _prod_row.get("index_value") or "")
        )
        _sel_coating_default = (
            _prod_row.get("coating_type")
            if _using_central_selector and str(_prod_row.get("coating_type") or "").strip()
            else (_lp_cur.get("coating") or _prod_row.get("coating_type") or "")
        )
        _sel_treatment_default = (
            _prod_row.get("material")
            if _using_central_selector and str(_prod_row.get("material") or "").strip()
            else (_lp_cur.get("treatment") or _lp_cur.get("material") or _prod_row.get("material") or "")
        )
        if _using_central_selector:
            _pcd_spec_sig = "|".join(str(x or "") for x in (
                _prod_row.get("product_id"),
                _sel_index_default,
                _sel_coating_default,
                _sel_treatment_default,
            ))
            if st.session_state.get("pcd_spec_sig") != _pcd_spec_sig:
                st.session_state["pcd_spec_sig"] = _pcd_spec_sig
                st.session_state["pcd_index"] = str(_sel_index_default or "")
                st.session_state["pcd_coating"] = str(_sel_coating_default or "")
                st.session_state["pcd_treatment"] = str(_sel_treatment_default or "")
        _new_index = _li1.text_input(
            "Index",
            value=str(_sel_index_default or ""),
            key="pcd_index",
        )
        _new_coating = _li2.text_input(
            "Coating",
            value=str(_sel_coating_default or ""),
            key="pcd_coating",
        )
        _new_treatment = _li3.text_input(
            "Treatment / Material",
            value=str(_sel_treatment_default or ""),
            key="pcd_treatment",
            help="e.g. Clear, Photochromic, Tinted",
        )

    # ── Power confirmation panel (F6) ────────────────────────────────────
    # Power fields (sph/cyl/axis/add_power) are NOT auto-copied or auto-cleared
    # on product change. Show the current power and force an explicit decision
    # so stale power never silently stays on a new product.
    _cur_sph  = line.get("sph")
    _cur_cyl  = line.get("cyl")
    _cur_axis = line.get("axis")
    _cur_add  = line.get("add_power")
    _has_power = any(v is not None and v != 0 for v in [_cur_sph, _cur_cyl, _cur_axis, _cur_add])

    if _has_power:
        def _pfmt(v):
            try:
                f = float(v)
                return f"{f:+.2f}" if f != 0 else "0.00"
            except Exception:
                return str(v) if v is not None else "—"

        st.info(
            f"⚠️ **Power on existing line:** "
            f"SPH {_pfmt(_cur_sph)}  CYL {_pfmt(_cur_cyl)}  "
            f"AXIS {int(_cur_axis) if _cur_axis else '—'}  "
            f"ADD {_pfmt(_cur_add) if _cur_add else '—'}"
        )
        _power_action = st.radio(
            "Power after product change",
            options=["Keep current power", "Clear power (set to zero)"],
            index=0,
            key="pcd_power_action",
            help="Confirm whether the existing power applies to the new product.",
        )
    else:
        _power_action = "Keep current power"  # no power to worry about

    # ── Preview ───────────────────────────────────────────────────────
    _preview_parts = [str(_prod_row["product_name"])]
    if _new_index:    _preview_parts.append(f"Idx {_new_index}")
    if _new_coating:  _preview_parts.append(_new_coating)
    if _new_treatment and not _is_frame_sel: _preview_parts.append(_new_treatment)
    st.success("Selected: " + " | ".join(_preview_parts))

    _pa1, _pa2 = st.columns(2)

    with _pa1:
        if st.button("✅ Apply Change", type="primary",
                     use_container_width=True, key="pcd_apply"):
            try:
                from modules.sql_adapter import run_write as _pcd_rw, run_query as _pcd_rq

                # ── Update lens_params with new index/coating/treatment ────
                _lp_new = dict(_lp_cur) if not _is_frame_sel else {}
                if _new_index:    _lp_new["lens_index"] = _new_index; _lp_new["index"] = _new_index
                if _new_coating:  _lp_new["coating"]    = _new_coating
                if _new_treatment:_lp_new["treatment"]  = _new_treatment; _lp_new["material"] = _new_treatment

                # Product changed: clear stale production/allocation metadata.
                _lp_new["manufacturing_route"] = None
                _lp_new["batch_allocation"]    = []
                _lp_new["batch_status"]        = "PENDING"
                _lp_new.pop("surfacing_data", None)
                if _pcd_selector_result and _pcd_selector_result.get("selected_sku"):
                    _lp_new["batch_no"] = str(_pcd_selector_result.get("selected_sku") or "")
                    _lp_new["selected_sku"] = str(_pcd_selector_result.get("selected_sku") or "")
                    _lp_new["stock_status"] = str(_pcd_selector_result.get("stock_status") or "")
                    _lp_new["available_qty"] = int(_pcd_selector_result.get("available_qty") or 0)
                    _lp_new["selector_source"] = "ui_product_selector"

                _new_pid = str(_prod_row["product_id"])
                _old_alloc_line = dict(line)
                _old_alloc_line["lens_params"] = dict(_lp_cur) if isinstance(_lp_cur, dict) else _lp_cur

                # ── Mutate in-memory line FIRST so pricing/discount engines see new product ──
                line["product_id"]   = _new_pid
                line["product_name"] = str(_prod_row["product_name"])
                line["brand"]        = str(_prod_row.get("brand", ""))
                line["main_group"]   = str(_prod_row.get("main_group", ""))
                line["material"]     = str(_prod_row.get("material", ""))
                line["lens_params"]  = _lp_new
                line["manufacturing_route"] = None
                line["batch_allocation"]    = []
                line["allocated_qty"]       = 0
                line["batch_status"]        = "PENDING"
                line["suggested_allocation"] = None

                # Clear old product discount attribution before new-rule evaluation.
                line["discount_percent"] = 0.0
                line["discount_amount"]  = 0.0
                line["discount_rule"]    = ""
                line["applied_rule_ids"] = ""

                # ── Power: apply staff decision from power_action radio (F6) ──
                # "Clear power" zeroes all four fields on the line so the new
                # product starts fresh. "Keep" leaves them untouched (staff
                # confirmed they still apply).
                if _power_action == "Clear power (set to zero)":
                    line["sph"]       = None
                    line["cyl"]       = None
                    line["axis"]      = None
                    line["add_power"] = None
                    _lp_new["power_cleared_on_product_change"] = True
                else:
                    _lp_new.pop("power_cleared_on_product_change", None)

                # ── Re-resolve price + GST for the new product ──
                _new_unit_price, _new_gst_pct = _bo_repricing_for_product(_new_pid, line, order)
                if _new_unit_price <= 0 and _using_central_selector:
                    _ot_pcd_price = str(order.get("order_type") or "RETAIL").upper()
                    _selector_price = (
                        float(_prod_row.get("mrp") or 0)
                        if _ot_pcd_price == "RETAIL"
                        else float(_prod_row.get("selling_price") or _prod_row.get("mrp") or 0)
                    )
                    if _selector_price > 0:
                        _new_unit_price = _selector_price
                        _new_gst_pct = float(_prod_row.get("gst_percent") or _new_gst_pct or 0)
                        _lp_new["price_source"] = str(_prod_row.get("_price_source") or "ui_product_selector")
                line["unit_price"]  = _new_unit_price
                line["gst_percent"] = _new_gst_pct
                _qty_pcd = int(line.get("billing_qty") or line.get("quantity") or 1)
                line["quantity"] = _qty_pcd
                line["total_price"]   = round(_new_unit_price * _qty_pcd, 2)
                line["billing_total"] = line["total_price"]

                # ── Re-apply discount engine for NEW brand/product ──
                try:
                    from modules.pricing.discount_flow import apply_order_discounts
                    _ot_pcd = str(order.get("order_type") or "RETAIL").upper()
                    _party_id = str(order.get("party_id") or "").strip()
                    if not _party_id:
                        _party_name = str(order.get("party_name") or order.get("patient_name") or "").strip()
                        if _party_name:
                            try:
                                _r = _pcd_rq(
                                    "SELECT id::text AS id FROM parties "
                                    "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                                    (_party_name,),
                                ) or []
                                if _r:
                                    _party_id = str(_r[0].get("id") or "")
                            except Exception as _party_lookup_err:
                                logger.warning(
                                    "[product_change_dialog] party lookup failed for discount context: %s",
                                    _party_lookup_err,
                                )
                    apply_order_discounts([line], party_id=_party_id, order_type=_ot_pcd)
                except Exception as _de:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[product_change_dialog] discount re-eval failed: {_de}"
                    )

                if float(line.get("discount_amount") or 0) > 0:
                    _lp_new["discount_status"] = "APPLIED"
                else:
                    _lp_new.pop("discount_status", None)

                # ── Persist complete pricing/discount state to DB ──
                _pcd_params = {
                    "pid": _new_pid,
                    "lp":  _pcd_json.dumps(_lp_new),
                    "up":  float(line.get("unit_price") or 0),
                    "tp":  float(line.get("billing_total") or line.get("total_price") or 0),
                    "gp":  float(line.get("gst_percent") or 0),
                    "ga":  float(line.get("gst_amount") or 0),
                    "dp":  float(line.get("discount_percent") or 0),
                    "da":  float(line.get("discount_amount") or 0),
                    "dr":  str(line.get("discount_rule") or ""),
                    "ari": str(line.get("applied_rule_ids") or ""),
                    "lid": line_id,
                    # F6: power fields — None when staff chose to clear
                    "sph":       line.get("sph"),
                    "cyl":       line.get("cyl"),
                    "axis":      line.get("axis"),
                    "add_power": line.get("add_power"),
                }

                # ── PRICE/DISCOUNT WRITE GUARD (product change) ──────────
                # Refuse to write a zero-price row. Mirrors price from a
                # sibling line on the same order if available; otherwise
                # blocks the save and shows a clear error so the user can
                # fix it instead of producing an unbillable row.
                _g_ok, _g_msg, _mp, _mg = _guard_line_price_before_write(
                    line, order, all_lines=None, context="product-change",
                )
                if not _g_ok:
                    return  # error already shown to user; do not save
                if _mp is not None:
                    _pcd_params["up"] = float(_mp)
                    _pcd_params["tp"] = round(
                        float(_mp) * float(line.get("quantity") or 1), 2
                    )
                    if _mg is not None:
                        _pcd_params["gp"] = float(_mg)

                _bo_release_stock_allocation_for_line(_old_alloc_line, "product_change")

                try:
                    _pcd_rw("""
                        UPDATE order_lines
                        SET product_id           = %(pid)s::uuid,
                            lens_params          = %(lp)s::jsonb,
                            unit_price           = %(up)s,
                            total_price          = %(tp)s,
                            billing_total        = %(tp)s,
                            gst_percent          = %(gp)s,
                            gst_amount           = %(ga)s,
                            discount_percent     = %(dp)s,
                            discount_amount      = %(da)s,
                            discount_rule        = %(dr)s,
                            applied_rule_ids     = %(ari)s,
                            allocated_qty        = 0,
                            batch_status         = 'PENDING',
                            suggested_allocation = NULL,
                            sph                  = %(sph)s,
                            cyl                  = %(cyl)s,
                            axis                 = %(axis)s,
                            add_power            = %(add_power)s
                        WHERE id = %(lid)s::uuid
                    """, _pcd_params)
                except Exception:
                    _pcd_rw("""
                        UPDATE order_lines
                        SET product_id           = %(pid)s::uuid,
                            lens_params          = %(lp)s::jsonb,
                            unit_price           = %(up)s,
                            total_price          = %(tp)s,
                            gst_percent          = %(gp)s,
                            gst_amount           = %(ga)s,
                            discount_percent     = %(dp)s,
                            discount_amount      = %(da)s,
                            discount_rule        = %(dr)s,
                            applied_rule_ids     = %(ari)s,
                            allocated_qty        = 0,
                            batch_status         = 'PENDING',
                            suggested_allocation = NULL
                        WHERE id = %(lid)s::uuid
                    """, _pcd_params)

                # Product/power changes can activate/deactivate supplier
                # schemes and cart/free offers on sibling lines. Re-run and
                # persist the full pricing stack before totals/challan gates
                # read this order again.
                try:
                    from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                    refresh_order_pricing_rules(order, persist=True)
                except Exception as _sync_err:
                    logger.warning("Backoffice product-change pricing sync failed: %s", _sync_err)

                # ── Re-assign manufacturing_route after product change ─────────
                # The product change clears manufacturing_route to None so the
                # workflow engine must re-evaluate it. Without this, the changed
                # line is invisible in both the 🏭 Supplier and 🧪 External Supplier
                # production tabs, and half-pair splits (one eye VENDOR, one INHOUSE)
                # never appear in the correct pipeline.
                try:
                    refresh_line_state(line)
                    # Persist the newly-computed route back to DB
                    _new_route = (
                        line.get("manufacturing_route")
                        or (line.get("lens_params") or {}).get("manufacturing_route")
                    )
                    if _new_route:
                        _lp_new["manufacturing_route"] = _new_route
                        _pcd_rw("""
                            UPDATE order_lines
                            SET lens_params = lens_params || %(lp_patch)s::jsonb
                            WHERE id = %(lid)s::uuid
                        """, {
                            "lp_patch": _pcd_json.dumps({
                                "manufacturing_route": _new_route
                            }),
                            "lid": line_id,
                        })
                        logger.info(
                            "[product_change] line %s manufacturing_route → %s",
                            line_id, _new_route,
                        )
                except Exception as _rls_err:
                    logger.warning(
                        "[product_change] refresh_line_state failed for line %s: %s",
                        line_id, _rls_err,
                    )

                _bo_refresh_order_total_value(order_id)

                # ── Clear all relevant caches ──
                try:
                    from modules.backoffice.order_loader import (
                        load_single_order, load_orders_from_database, load_orders_summary
                    )
                    for _fn in (load_single_order, load_orders_from_database, load_orders_summary):
                        try: _fn.clear()
                        except Exception: pass
                except Exception:
                    pass
                try:
                    from modules.backoffice.backoffice_helpers import load_orders_from_database as _boh_load
                    _boh_load.clear()
                    st.session_state["bo_orders_loaded"] = False
                except Exception:
                    pass

                st.session_state["bo_product_change_modal"] = {"active": False}

                # ── Force fresh DB reload on next render ──────────────────
                # bo_active_orders holds the cached order dict with stale line data.
                # Removing it here means render_order_detail falls through to
                # load_single_order(order_id) on the next rerun — guaranteeing
                # the UI shows the just-persisted product/price/discount values.
                #
                # FIX (Order-not-found after product change): load_single_order
                # needs the DB UUID, but bo_selected_order_id may be the display
                # order number. Capture the real DB id of THIS order before we
                # evict it from the cache, and stash it so the reload path can
                # resolve by UUID. Pure safety addition — no existing logic
                # changed, no lines removed.
                _real_db_id = None
                for _o in st.session_state.get("bo_active_orders", []):
                    if str(_o.get("id") or _o.get("order_id") or "") == str(order_id) \
                       or str(_o.get("order_no") or "") == str(order_id):
                        _real_db_id = _o.get("id") or _o.get("order_id")
                        break
                if _real_db_id:
                    st.session_state["bo_reload_db_id"] = str(_real_db_id)

                st.session_state["bo_active_orders"] = [
                    o for o in st.session_state.get("bo_active_orders", [])
                    if str(o.get("id") or o.get("order_id") or "") != str(order_id)
                ]
                st.session_state["bo_orders_loaded"] = False

                _disc_msg = ""
                _da_show = float(line.get("discount_amount") or 0)
                if _da_show > 0:
                    _rule_show = str(line.get("discount_rule") or "rule")
                    _disc_msg = f" · Discount applied: {_rule_show} ({_da_show:.2f})"
                st.success(f"✅ Product changed to {_prod_row['product_name']}{_disc_msg}")
                st.rerun()

            except Exception as _pe:
                st.error(f"Product change failed: {_pe}")

    with _pa2:
        if st.button("Cancel", use_container_width=True, key="pcd_cancel"):
            st.session_state["bo_product_change_modal"] = {"active": False}
            st.rerun()


def show_supplier_order_section(order: Dict):
    """
    Enhanced supplier order section with diagnostics
    Shows button if vendor lines exist, otherwise shows why not
    
    SUPPORTS BOTH:
    - VENDOR route (external suppliers)
    - EXTERNAL_LAB route (lab orders that need supplier procurement)
    """
    from modules.supplier_orders_management import create_supplier_order_from_lines
    
    # Get ALL lines
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    all_lines.extend(order.get('service_lines', []))
    
    #  FIX: Include BOTH VENDOR and EXTERNAL_LAB routes
    vendor_lines = [
        line for line in all_lines 
        if line.get('manufacturing_route') in ['VENDOR', 'EXTERNAL_LAB'] and
        not line.get('supplier_order_id')  # Not already ordered
    ]
    
    # Always show section for visibility
    st.markdown("---")
    st.markdown("###  Supplier Orders")
    
    if vendor_lines:
        if st.button(
            f"📦 Create Supplier Order ({len(vendor_lines)} items)",
            type="primary",
            use_container_width=True,
            key="create_supplier_order_main_btn"
        ):
            create_supplier_order_from_lines(order, vendor_lines)
    else:
        # No vendor lines - show diagnostics
        if not all_lines:
            st.warning(" No order lines found in this order")
        else:
            # Check routing
            routes = {}
            already_ordered = 0
            
            for line in all_lines:
                route = line.get('manufacturing_route', 'NOT_SET')
                routes[route] = routes.get(route, 0) + 1
                
                if line.get('supplier_order_id'):
                    already_ordered += 1
            
            # Show diagnostics
            st.info(f" **Order has {len(all_lines)} line items:**")
            
            for route, count in routes.items():
                if route == 'VENDOR':
                    st.success(f" {count} items routed to VENDOR")
                elif route == 'EXTERNAL_LAB':
                    st.success(f" {count} items routed to EXTERNAL_LAB (supplier procurement needed)")
                elif route == 'STOCK':
                    st.info(f" {count} items routed to STOCK (from inventory)")
                elif route == 'INHOUSE':
                    st.info(f" {count} items routed to INHOUSE (manufacture internally)")
                elif route == 'LAB_ORDER':
                    st.info(f" {count} items routed to LAB_ORDER")
                else:
                    st.warning(f" {count} items with route: {route}")
            
            if already_ordered > 0:
                st.success(f" {already_ordered} items already have supplier orders")
            
            # Help message
            if 'VENDOR' not in routes and 'EXTERNAL_LAB' not in routes:
                st.info("""
                 **No vendor items in this order**
                
                Items are routed to vendors when:
                - Product is not in stock
                - Product cannot be manufactured in-house
                - Specific vendor is required
                
                To route items to vendor, check the manufacturing route settings.
                """)
            elif already_ordered == (routes.get('VENDOR', 0) + routes.get('EXTERNAL_LAB', 0)):
                st.success(" All vendor items already have supplier orders created")
    
    st.markdown("---")


# ============================================================================
# ============================================================================

def generate_job_cards(order: Dict):
    """
    Generate job cards with surfacing support.

    Layout logic:
    - If the order has exactly one R and one L line for the same product,
      render them SIDE BY SIDE (two columns) — this is the common progressive case.
    - Otherwise fall back to one expander per line (different products, or
      single-eye, or more than 2 lines).
    """

    from modules.documents.job_card_surfacing import (
        render_surfacing_job_card,
        render_job_card_print,
    )

    st.markdown("---")
    st.markdown("### 🔧 In-House Job Cards")

    inhouse_lines = order.get("inhouse_lines", [])

    if not inhouse_lines:
        st.warning("No in-house items requiring job cards")
        return

    # ── Group lines: try to pair R + L for same product ──────────────
    # Build a dict keyed by product_id (or product_name as fallback).
    # If a product has both R and L, show them side-by-side.
    from collections import defaultdict
    product_groups: dict = defaultdict(dict)   # {product_key: {"R": line, "L": line, ...}}

    for line in inhouse_lines:
        pk = line.get("product_id") or line.get("product_name") or "unknown"
        side = (line.get("eye_side") or "").upper().strip()
        if side in ("R", "L"):
            product_groups[pk][side] = line
        else:
            # Non-eye-specific — keep separately with a unique key
            product_groups[f"{pk}__{id(line)}"]["X"] = line

    rendered_line_ids = set()

    # ── Render paired R+L side-by-side, then any remaining singles ────
    for pk, sides in product_groups.items():
        r_line = sides.get("R")
        l_line = sides.get("L")

        if r_line is not None and l_line is not None:
            # ── Paired: show R and L in two columns ──────────────────
            product_name = r_line.get("product_name", "Unknown Product")

            with st.expander(
                f"👁️ {product_name} — Right & Left Eye",
                expanded=True,
            ):
                col_r, col_divider, col_l = st.columns([10, 1, 10])

                with col_r:
                    st.markdown(
                        "<div style='background:#1a3a2a;border-radius:8px;padding:6px 14px;"
                        "margin-bottom:10px;'>"
                        "<span style='color:#4ade80;font-weight:700;font-size:1rem;'>"
                        "👁 RIGHT EYE</span></div>",
                        unsafe_allow_html=True,
                    )
                    render_surfacing_job_card(r_line, order)
                    if r_line.get("surfacing_data"):
                        _r_pk = f"bo_jc_print_r_{r_line.get('line_id','')[:8]}"
                        if st.button("🖨 Print Job Card — R", key=_r_pk+"_btn",
                                     use_container_width=True):
                            render_job_card_print(r_line, order)

                with col_divider:
                    st.markdown(
                        "<div style='border-left:2px dashed #334155;height:100%;margin:0 auto;width:2px;'></div>",
                        unsafe_allow_html=True,
                    )

                with col_l:
                    st.markdown(
                        "<div style='background:#1a2a3a;border-radius:8px;padding:6px 14px;"
                        "margin-bottom:10px;'>"
                        "<span style='color:#60a5fa;font-weight:700;font-size:1rem;'>"
                        "👁 LEFT EYE</span></div>",
                        unsafe_allow_html=True,
                    )
                    render_surfacing_job_card(l_line, order)
                    if l_line.get("surfacing_data"):
                        _l_pk = f"bo_jc_print_l_{l_line.get('line_id','')[:8]}"
                        if st.button("🖨 Print Job Card — L", key=_l_pk+"_btn",
                                     use_container_width=True):
                            render_job_card_print(l_line, order)

            rendered_line_ids.add(id(r_line))
            rendered_line_ids.add(id(l_line))

        else:
            # ── Single line (one eye only, or non-eye-specific) ───────
            line = r_line or l_line or list(sides.values())[0]
            if id(line) in rendered_line_ids:
                continue
            eye_label = (line.get("eye_side") or "").upper().strip()
            eye_display = {"R": "Right Eye", "L": "Left Eye"}.get(eye_label, eye_label or "Lens")
            product_name = line.get("product_name", "Unknown Product")

            with st.expander(f"👁 {product_name} — {eye_display}", expanded=True):
                render_surfacing_job_card(line, order)
                if line.get("surfacing_data"):
                    _s_pk = f"bo_jc_print_s_{line.get('line_id','')[:8]}"
                    if st.button("🖨 Print Job Card", key=_s_pk+"_btn",
                                 use_container_width=True):
                        render_job_card_print(line, order)

            rendered_line_ids.add(id(line))

def generate_lab_orders(order: Dict):
    """Generate lab orders for external items"""
    st.markdown("---")
    st.markdown("###  Lab Orders")
    
    lab_lines = order.get('lab_order_lines', [])
    
    if not lab_lines:
        st.warning("No lab order items")
        return
    
    # Lab order summary
    st.markdown("#### Lab Order Summary")
    
    lab_data = []
    for line in lab_lines:
        #  FIX: Calculate pending qty for lab orders
        billing_qty = int(line.get('billing_qty', 0))
        allocated = int(line.get('allocated_qty', 0))
        pending = max(0, billing_qty - allocated)
        
        lab_data.append({
            'Product': line.get('product_name', 'N/A'),
            'Brand': line.get('brand', 'N/A'),
            'Eye': line.get('eye_side', 'N/A'),
            'SPH': fmt_signed(line.get('sph')),
            'CYL': fmt_signed(line.get('cyl')),
            'AXIS': line.get('axis', 'N/A'),
            'Qty': pending
        })
    
    st.dataframe(pd.DataFrame(lab_data))
    
    # Lab selection
    lab_name = st.selectbox(
        "Select Lab",
        ["Lab A - Premium Optics", "Lab B - Standard Optics", "Lab C - Express Optics"],
        key='lab_select'
    )
    
    expected_delivery = st.date_input(
        "Expected Delivery Date",
        value=datetime.date.today() + datetime.timedelta(days=7),
        key='lab_delivery_date'
    )
    
    from modules.utils.submit_guard import is_locked, guarded_submit
    if st.button(" Send Lab Order", type="primary", use_container_width=True,
                 disabled=is_locked("lab_order")):
        with guarded_submit("lab_order") as _allowed:
            if not _allowed:
                st.stop()
            try:
                from modules.backoffice.audit_logger import audit, AuditAction
                audit(AuditAction.LAB_ORDER_SENT, entity="orders",
                      entity_id=order.get("order_id"),
                      payload={"lab": lab_name, "delivery": str(expected_delivery)})
            except Exception:
                pass
            st.success(f" Lab order sent to {lab_name}")
            st.info(f"Expected delivery: {expected_delivery}")


def generate_labels(order: Dict):
    """Generate labels for stock items"""
    st.markdown("---")
    st.markdown("###  Product Labels")

    stock_lines = order.get('stock_lines', [])

    if not stock_lines:
        st.warning("No stock items requiring labels")
        return

    for idx, line in enumerate(stock_lines, 1):
        col1, col2 = st.columns([3, 2])

        with col1:
            st.markdown(f"#### Label #{idx}")
            st.text(f"Product: {line.get('product_name', 'N/A')}")
            st.text(f"Brand: {line.get('brand', 'N/A')}")
            st.text(f"Patient: {order.get('patient_name', 'N/A')}")
            st.text(f"Eye: {line.get('eye_side', 'N/A')}")

            #  FIX: Always render power in SPH CYL AXIS ADD format
            if line.get('sph') is not None:
                power_str = f"Power: SPH {fmt_signed(line.get('sph'))} | CYL {fmt_signed(line.get('cyl'))}"
                
                # Add AXIS if cylinder exists
                if abs(line.get('cyl') or 0) > 0.01:
                    power_str += f" | AXIS {line.get('axis', 'N/A')}"
                
                # Add ADD if present
                if line.get('add_power') is not None:
                    power_str += f" | ADD {fmt_signed(line.get('add_power'))}"
                
                st.text(power_str)

            # ===== BATCH INFO =====
            if line.get('batch_allocation'):
                batch_info = ", ".join(
                    b.get('batch_no', 'N/A') for b in line.get('batch_allocation', [])
                )
                st.text(f"Batch(es): {batch_info}")

        with col2:
            st.text(f"Order: {get_display_order_id(order)}")
            order_date = order.get("created_at")

            if order_date:
                if hasattr(order_date, "strftime"):
                    date_str = order_date.strftime("%Y-%m-%d")
                else:
                    date_str = str(order_date)[:10]
            else:
                date_str = "N/A"


            st.text(f"Date: {date_str}")

            st.text(f"Qty: {line.get('billing_qty', 0)}")

        st.markdown("---")

    st.success(f" {len(stock_lines)} label(s) ready for printing")

def render_qty_finalization_ui(line: Dict, line_idx: int, order: Dict):
    """
    Safe stub to prevent import errors.
    Quantity editing is now handled in main UI.
    """
    st.markdown("####  Quantity")

    current_qty = int(line.get("billing_qty", 1))

    new_qty = st.number_input(
        "Billing Quantity",
        min_value=1,
        value=current_qty,
        step=1,
        key=f"qty_stub_{line_idx}"
    )

    if new_qty != current_qty:
        _bo_release_stock_allocation_for_line(line, "quantity_change")
        line["billing_qty"] = new_qty

        #  CRITICAL FIX  Always update totals
        recalculate_order_totals(order)

        # Reset allocation so workflow recomputes
        line["batch_allocation"] = []
        line["allocated_qty"] = 0
        line["batch_status"] = "PENDING"

        refresh_line_state(line)

        st.success(f" Quantity updated to {new_qty}")
        st.rerun()


# ============================================================================
# ============================================================================

# ============================================================================
# ORDER-SUMMARY INLINE DELETE
# ----------------------------------------------------------------------------
# Renders a 🗑 button cell for a single summary row. Reuses the canonical
# soft-delete SQL pattern already used by the line-card 🗑 Delete Line
# expander (UPDATE order_lines SET is_deleted=TRUE, status='CANCELLED' …),
# so deletes from the Summary and from the Line Card are identical and the
# existing ↩ Restore Deleted Lines panel restores either of them.
# Two-click confirm — accidental tap-to-delete is prevented by a session
# flag, mirroring the line-card pattern.
# ============================================================================
def _bo_summary_row_delete_button(line: Dict, order: Dict, cell) -> None:
    """Render delete (+ inline confirm) inside `cell` for a summary row."""
    try:
        _lid = str(line.get("line_id") or line.get("id") or "").strip()
        if not _lid or len(_lid) < 10:
            cell.write("—")
            return
        # Block delete if line is already billed (mirrors line-card guard).
        if float(line.get("billed_qty") or 0) > 0:
            cell.write("🔒")
            cell.caption("billed")
            return
        _ck = f"bo_summary_del_confirm_{_lid}"
        if not st.session_state.get(_ck):
            if cell.button("🗑", key=f"bo_summary_del_{_lid}", help="Delete this line"):
                st.session_state[_ck] = True
                st.rerun()
        else:
            _c1, _c2 = cell.columns(2)
            if _c1.button("✅", key=f"bo_summary_del_yes_{_lid}",
                          help="Confirm delete", type="primary"):
                try:
                    from modules.sql_adapter import run_write as _rw_sd
                    _rw_sd(
                        """
                        UPDATE order_lines
                        SET is_deleted  = TRUE,
                            status      = 'CANCELLED',
                            deleted_at  = NOW(),
                            deleted_by  = %(who)s
                        WHERE id = %(lid)s::uuid
                        """,
                        {"lid": _lid, "who": str(st.session_state.get("user_name") or "backoffice")},
                    )
                    try:
                        _bo_refresh_order_total_value(
                            str(order.get("id") or order.get("order_id") or "")
                        )
                    except Exception:
                        pass
                    # also mirror in-memory so the very next render is correct
                    line["is_deleted"] = True
                    st.session_state[_ck] = False
                    st.success("Line deleted")
                    st.rerun()
                except Exception as _de:
                    st.error(f"Delete failed: {_de}")
                    st.session_state[_ck] = False
            if _c2.button("✖", key=f"bo_summary_del_no_{_lid}", help="Cancel"):
                st.session_state[_ck] = False
                st.rerun()
    except Exception as _be:
        # never crash a render row
        try:
            cell.write("—")
            import logging
            logging.getLogger(__name__).warning(
                "[BO summary delete] render failed: %s", _be
            )
        except Exception:
            pass


def render_order_detail():
    """
    Render detailed order view with all workflow components

    CRITICAL FIXES:
    1. Power editing triggers complete workflow
    2. Allocation window appears automatically
    3. Billing updates in real-time
    4. Ophthalmic job cards render correctly
    """

    # 
    # 1. Resolve order_id
    # 
    order_id = st.session_state.bo_selected_order_id

    if not order_id:
        st.warning("No order selected")
        if st.button(" Back to Dashboard", key="back_to_dashboard_no_order"):
            st.session_state.bo_view_mode = 'dashboard'
            st.rerun()
        return

    # Production cards may open Backoffice with display refs such as
    # R/2627/0012-C or composite group keys. Resolve once here so every
    # downstream loader/query receives the real order UUID.
    try:
        from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid_bo_detail
        _resolved_order_id = _resolve_order_uuid_bo_detail(order_id) or ""
        if _resolved_order_id:
            order_id = _resolved_order_id
            st.session_state.bo_selected_order_id = _resolved_order_id
    except Exception:
        pass

    # Reset assignment panel state whenever a DIFFERENT order is opened
    _last_oid_key = "_bo_assignment_last_order_id"
    if st.session_state.get(_last_oid_key) != order_id:
        st.session_state[_last_oid_key]          = order_id
        st.session_state["bo_assignments"]        = {}
        st.session_state["bo_assignments_locked"] = False
        st.session_state["bo_shift_target"]       = None

    # 
    # 2. Find order in active list
    # 
    order = None
    for o in st.session_state.bo_active_orders:
        if (
            get_display_order_id(o) == order_id
            or str(o.get("id", "")) == str(order_id)
            or str(o.get("order_id", "")) == str(order_id)
            or str(o.get("order_no", "")) == str(order_id)
        ):
            order = o
            break

    # Lazy load: if we only have a summary row (no lines), load full detail now
    if order is not None and not order.get("lines") and not order.get("_existed_in_db"):
        try:
            from modules.backoffice.order_loader import load_single_order as _lso
            _full = _lso(str(order.get("id") or order.get("order_id") or order_id))
            if _full:
                order = _full
                # Update the session state entry so next open is instant
                for _i, _o in enumerate(st.session_state.bo_active_orders):
                    if (
                        get_display_order_id(_o) == order_id
                        or str(_o.get("id", "")) == str(order_id)
                        or str(_o.get("order_id", "")) == str(order_id)
                        or str(_o.get("order_no", "")) == str(order_id)
                    ):
                        st.session_state.bo_active_orders[_i] = _full
                        break
        except Exception as _le:
            pass  # fall through with summary row — UI will show what it has

    if not order:
        # Not in session list — try direct DB load
        try:
            from modules.backoffice.order_loader import load_single_order as _lso2
            order = _lso2(str(order_id))
        except Exception:
            pass

    if not order:
        # FIX (Order-not-found after product change): the product-change flow
        # evicts the order from cache and stashes its REAL DB UUID. If the
        # lookup above failed because order_id is a display number, retry the
        # DB load with that UUID. One-shot — clear after use.
        _stashed = st.session_state.pop("bo_reload_db_id", None)
        if _stashed:
            try:
                from modules.backoffice.order_loader import load_single_order as _lso3
                order = _lso3(str(_stashed))
            except Exception:
                pass

    if not order:
        st.error("Order not found")
        if st.button(" Back to Dashboard", key="back_to_dashboard_order_not_found"):
            st.session_state.bo_view_mode = 'dashboard'
            st.rerun()
        return

    if str(order.get("order_type") or "").upper() == "CONSULTATION":
        st.info(
            "Consultation-only visit is closed in the Consultation module. "
            "Receipts are posted directly to registers/accounts and do not enter Backoffice."
        )
        if st.button(" Back to Dashboard", key="back_to_dashboard_consultation_order"):
            st.session_state.bo_view_mode = 'dashboard'
            st.session_state.bo_selected_order_id = None
            st.rerun()
        return

    # 
    # 3. Lazy refresh  run workflow engine on lines that need it
    #    (lines loaded from DB have _needs_refresh=True)
    # 
    needs_rerun = False
    for line in order.get("lines", []):
        if line.get("_needs_refresh"):
            try:
                # refresh_line_state handles routing (STOCK/VENDOR) AND
                # recalculates billing_total via update_line_billing internally.
                # It does NOT change unit_price — that came from retail punching.
                refresh_line_state(line)
            except Exception as e:
                import logging
                logging.warning(f"[BO] refresh_line_state failed for line {line.get('product_name')}: {e}")
            finally:
                line["_needs_refresh"] = False
            needs_rerun = True

    # ── READ-TIME PRICE AUTO-HEAL ────────────────────────────────────────
    # Heal any line that was previously saved with unit_price=0 while a
    # sibling line on the SAME order has a valid price for the SAME
    # product. Mirrors price + gst + discount_percent + rule from the
    # priced sibling, recomputes discount_amount + totals from the mirror,
    # and PERSISTS the repair to the DB in a single UPDATE per healed line.
    # After the first open of a broken order the row is fixed in storage,
    # and the write-guard prevents recurrence on future edits.
    # Auditable: every heal is logged with from/to values.
    try:
        _lines_for_heal = order.get("lines", []) or []
        _heal_lid_done = set()
        for _ln in _lines_for_heal:
            if bool(_ln.get("is_deleted")):
                continue
            _up_cur = float(_ln.get("unit_price") or 0)
            _qty_cur = float(_ln.get("quantity") or _ln.get("qty") or 0)
            if _up_cur > 0 or _qty_cur <= 0:
                continue
            _pid = str(_ln.get("product_id") or "")
            if not _pid:
                continue
            # find a priced sibling with the same product
            _src = None
            for _sib in _lines_for_heal:
                if _sib is _ln or bool(_sib.get("is_deleted")):
                    continue
                if str(_sib.get("product_id") or "") != _pid:
                    continue
                if float(_sib.get("unit_price") or 0) > 0:
                    _src = _sib
                    break
            if _src is None:
                # no sibling to mirror from — leave the row, validator will
                # surface it to the user (correct behaviour; we never invent)
                continue

            _new_up   = float(_src.get("unit_price") or 0)
            _new_gst  = float(_src.get("gst_percent") or _ln.get("gst_percent") or 0)
            _new_dpct = float(_src.get("discount_percent") or 0)
            _new_drule= str(_src.get("discount_rule") or "")
            _qty_int  = int(_qty_cur)
            _gross    = round(_new_up * _qty_int, 2)
            _new_damt = round(_gross * _new_dpct / 100, 2) if _new_dpct > 0 else 0.0
            _new_net  = round(max(0.0, _gross - _new_damt), 2)
            # GST is per-PCS-net basis, same convention used elsewhere
            _gst_amt  = round(_new_net * _new_gst / 100, 2)

            # Mutate in-memory line so subsequent render shows healed values
            _ln["unit_price"]      = _new_up
            _ln["gst_percent"]     = _new_gst
            _ln["discount_percent"]= _new_dpct
            _ln["discount_rule"]   = _new_drule
            _ln["discount_amount"] = _new_damt
            _ln["total_price"]     = _new_net
            _ln["billing_total"]   = _new_net
            _ln["gst_amount"]      = _gst_amt

            # Persist the repair to the DB (one UPDATE per healed line).
            _lid = str(_ln.get("line_id") or _ln.get("id") or "")
            if _lid and _lid not in _heal_lid_done:
                _heal_lid_done.add(_lid)
                try:
                    from modules.sql_adapter import run_write as _rw_heal
                    _rw_heal(
                        """
                        UPDATE order_lines
                        SET unit_price       = %(up)s,
                            gst_percent      = %(gp)s,
                            total_price      = %(tp)s,
                            billing_total    = %(tp)s,
                            gst_amount       = %(ga)s,
                            discount_percent = %(dp)s,
                            discount_amount  = %(da)s,
                            discount_rule    = %(dr)s
                        WHERE id = %(lid)s::uuid
                          AND COALESCE(unit_price, 0) = 0
                        """,
                        {
                            "up": _new_up,    "gp": _new_gst,
                            "tp": _new_net,   "ga": _gst_amt,
                            "dp": _new_dpct,  "da": _new_damt,
                            "dr": _new_drule, "lid": _lid,
                        },
                    )
                    import logging
                    logging.getLogger(__name__).warning(
                        "[BO] price auto-heal: order=%s line=%s eye=%s "
                        "product_id=%s mirrored unit_price 0 -> %.2f from "
                        "sibling line; persisted to DB.",
                        order.get("order_no"), _lid,
                        _ln.get("eye_side"), _pid, _new_up,
                    )
                    needs_rerun = True  # totals must re-sum after heal
                except Exception as _he:
                    # Heal write failed — do NOT swallow silently. The
                    # in-memory mirror still benefits this render, but log
                    # so ops can see the persistence failure.
                    import logging, traceback
                    logging.getLogger(__name__).warning(
                        "[BO] price auto-heal DB write failed (in-memory "
                        "still mirrored): %s\n%s", _he, traceback.format_exc()
                    )
    except Exception as _heal_top:
        # Heal is best-effort, never blocks render. Log loudly.
        import logging
        logging.getLogger(__name__).warning(
            "[BO] price auto-heal scan failed: %s", _heal_top
        )

    # ── DISCOUNT RECONCILIATION FROM DB ───────────────────────────────────
    # Symptom this fixes: badge / Order Summary discount shows ₹0.00 even
    # though the DB has discount_amount > 0 for live lines (e.g. L line
    # ₹21.50 in DB but memory shows 0). Root cause: refresh_line_state +
    # update_line_billing + auto-refresh discount engine each touch
    # billing_total / gst / etc. and somewhere in that chain in-memory
    # discount_amount gets reset before the badge sums it. We don't fight
    # each intermediate writer — instead we read DB truth once after all
    # the refreshers have run, and reconcile if memory drifted.
    # Strict: only writes from DB→memory, never the other way; only when
    # DB value is higher than memory (so a deliberate in-session edit that
    # legitimately reduced the discount is not clobbered).
    try:
        _live_lids = [
            str(_l.get("line_id") or _l.get("id") or "")
            for _l in (order.get("lines") or [])
            if str(_l.get("line_id") or _l.get("id") or "")
            and not bool(_l.get("is_deleted"))
        ]
        if _live_lids:
            from modules.sql_adapter import run_query as _rq_dr
            _db_disc_rows = _rq_dr(
                """
                SELECT id::text AS lid,
                       COALESCE(discount_amount, 0)  AS da,
                       COALESCE(discount_percent, 0) AS dp,
                       COALESCE(discount_rule, '')   AS dr
                FROM order_lines
                WHERE id::text = ANY(%(ids)s)
                """,
                {"ids": _live_lids},
            ) or []
            _by_lid = {str(r.get("lid")): r for r in _db_disc_rows}
            _reconciled = 0
            for _ln in (order.get("lines") or []):
                _lid_r = str(_ln.get("line_id") or _ln.get("id") or "")
                if not _lid_r or bool(_ln.get("is_deleted")):
                    continue
                _db_row = _by_lid.get(_lid_r)
                if not _db_row:
                    continue
                _db_da = float(_db_row.get("da") or 0)
                _mem_da = float(_ln.get("discount_amount") or 0)
                # Trust DB only if it is strictly higher — i.e. memory has
                # lost the discount. Never lower a discount based on DB.
                if _db_da > _mem_da + 0.01:
                    _ln["discount_amount"]  = _db_da
                    _ln["discount_percent"] = float(_db_row.get("dp") or 0)
                    _ln["discount_rule"]    = str(_db_row.get("dr") or "")
                    # Recompute net so billing_total reflects the restored
                    # discount (intermediate writers may have set it gross).
                    _gross = round(
                        float(_ln.get("unit_price") or 0)
                        * int(_ln.get("quantity") or _ln.get("billing_qty") or 1),
                        2,
                    )
                    _net = round(max(0.0, _gross - _db_da), 2)
                    _ln["billing_total"] = _net
                    _ln["total_price"]   = _net
                    # GST recompute on net (wholesale = on top, retail = incl)
                    try:
                        _ot_r = str(order.get("order_type") or "RETAIL").upper()
                        _gp_r = float(_ln.get("gst_percent") or 0)
                        if _gp_r > 0 and _net > 0:
                            if _ot_r == "RETAIL":
                                _ln["gst_amount"] = round(_net - (_net / (1 + _gp_r/100)), 2)
                            else:
                                _ln["gst_amount"] = round(_net * _gp_r / 100, 2)
                    except Exception:
                        pass
                    _reconciled += 1
            if _reconciled:
                import logging
                logging.getLogger(__name__).warning(
                    "[BO] discount reconciliation: restored %d line(s) "
                    "from DB on order %s",
                    _reconciled, order.get("order_no"),
                )
                needs_rerun = True
    except Exception as _dr_e:
        import logging
        logging.getLogger(__name__).warning(
            "[BO] discount reconciliation failed: %s", _dr_e
        )

    if needs_rerun:
        # Recalculate order totals then re-categorize
        recalculate_order_totals(order)
        from .backoffice_helpers import categorize_order_lines, apply_schemes_to_order_lines, apply_cart_schemes_to_order_lines
        try:
            _scheme_party = str(order.get("party_id") or order.get("customer_id") or "")
            apply_schemes_to_order_lines(order, party_id=_scheme_party)
            apply_cart_schemes_to_order_lines(order, party_id=_scheme_party)
        except Exception:
            pass
        categorize_order_lines(order)
    
    # =====================================================
    # RENDER SIDEBAR (if available)
    # =====================================================
    if render_backoffice_sidebar is not None:
        try:
            render_backoffice_sidebar(order)
        except Exception as e:
            import logging
            logging.warning(f"[BO] Sidebar render failed: {e}")

    # Header
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.title(f"📋 Order: {get_display_label(order)}")

    # ── Order channel + party / patient identity (Issue 1) ───────────────
    # Display-only. Pulls from the existing order dict — no logic change,
    # no lines removed. Makes RETAIL / WHOLESALE / ONLINE + who the order is
    # for unmistakable at the top of the page.
    try:
        _ch = str(order.get("order_type") or order.get("source") or "").upper().strip()
        _ch_label = {
            "RETAIL": "🛍️ RETAIL",
            "WHOLESALE": "🏭 WHOLESALE",
            "ONLINE": "🌐 ONLINE",
            "BULK": "📦 BULK",
        }.get(_ch, _ch or "—")
        _ch_color = {
            "RETAIL": "#0ea5e9", "WHOLESALE": "#a855f7",
            "ONLINE": "#10b981", "BULK": "#f59e0b",
        }.get(_ch, "#64748b")
        _patient = str(order.get("patient_name") or "").strip()
        _party   = str(order.get("party_name") or order.get("customer_name") or "").strip()
        _bits = []
        if _party:
            _bits.append(f"Party: <b>{_party}</b>")
        if _patient and _patient.upper() != "N/A":
            _bits.append(f"Patient: <b>{_patient}</b>")
        _who = " &nbsp;·&nbsp; ".join(_bits) if _bits else "<i>No party / patient name on order</i>"
        st.markdown(
            f"<div style='margin:-8px 0 10px 0;padding:7px 12px;"
            f"background:{_ch_color}1a;border-left:4px solid {_ch_color};"
            f"border-radius:5px'>"
            f"<span style='background:{_ch_color};color:#fff;font-weight:700;"
            f"font-size:0.74rem;padding:2px 9px;border-radius:4px'>{_ch_label}</span>"
            f"&nbsp;&nbsp;<span style='color:#334155;font-size:0.86rem'>{_who}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    except Exception:
        pass  # identity banner is cosmetic — never block the page
    
    with col2:
        if st.button(" Back", use_container_width=True):
            st.session_state.bo_view_mode = 'dashboard'
            st.session_state.bo_editing_line = None
            st.session_state.bo_show_allocation_window = False
            st.rerun()
    
    # Order info
    col_a, col_b, col_c = st.columns(3)
    
    with col_a:
        st.metric("Patient", order.get('patient_name', 'N/A'))
    with col_b:
        # Show status + production sub-stage in brackets when in-house order.
        # For BILLED / CHALLANED / INVOICED orders the production stage is
        # complete — show the billing status only (no stale job stage appended).
        _disp_status  = order.get("status", "PENDING")
        _order_st_up  = str(_disp_status).upper()
        _billing_done = _order_st_up in (
            "BILLED", "CHALLANED", "INVOICED",
            "DISPATCHED", "DELIVERED", "CLOSED",
        )
        _inhouse_lns  = order.get("inhouse_lines") or []
        if _inhouse_lns and not _billing_done:
            # Only show production sub-stage when order is still in the
            # production / pre-billing phase. Once billed, job stages are frozen.
            try:
                from modules.sql_adapter import run_query as _rq_stg
                _jm_stgs = _rq_stg("""
                    SELECT ol.eye_side,
                           COALESCE(jm.current_stage, 'JOB_CREATED') AS stage
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    LEFT JOIN job_master jm ON jm.order_line_id = ol.id
                    WHERE o.order_no = %(ono)s
                      AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    ORDER BY ol.eye_side
                """, {"ono": order.get("order_no", "")})
                if _jm_stgs:
                    _STAGE_SHORT = {
                        "JOB_CREATED":       "Job Created",
                        "PRINTED":           "Printed",
                        "JOB_PRINTED":       "Printed",
                        "PRODUCTION_PICKED": "In Production",
                        "PRODUCTION_DONE":   "Production Done",
                        "INSPECTION":        "Inspection",
                        "BLANK_ALLOCATED":   "Blank Allocated",
                        "HARDCOAT_PICKED":   "Hardcoat Picked",
                        "HARDCOAT_DONE":     "Hardcoat Done",
                        "COLOURING_PICKED":  "Colouring Picked",
                        "COLOURING_DONE":    "Colouring Done",
                        "ARC_SENT":          "ARC Sent",
                        "ARC_RECEIVED":      "ARC Received",
                        "FINAL_QC":          "Final QC",
                        "READY_FOR_PACK":    "Ready for Pack",
                        "READY_TO_BILL":     "Ready to Bill",
                        "FITTING_PENDING":   "Fitting Pending",
                        "FITTING_DONE":      "Fitting Done",
                        "REJECTED":          "Rejected",
                        "BILLED":            "Billed",
                        "CANCELLED":         "Cancelled",
                    }
                    _parts = []
                    for _jr in _jm_stgs:
                        _e = str(_jr.get("eye_side") or "").upper()
                        _s = str(_jr.get("stage") or "JOB_CREATED").upper()
                        _short = _STAGE_SHORT.get(_s, _s.replace("_"," ").title())
                        _e_label = {"R":"RE","L":"LE"}.get(_e, _e)
                        _parts.append(f"{_e_label}: {_short}")
                    if _parts:
                        _disp_status = f"{order.get('status','PENDING')} ({' | '.join(_parts)})"
            except Exception:
                pass
        st.metric("Status", _disp_status)

    with col_c:
        order_date = order.get('created_at', '')
        if order_date:
            date_str = str(order_date)[:10]
        else:
            date_str = 'N/A'

        st.metric("Date", date_str)

    # ── Customer / Authenticity Card details ─────────────────────────────
    # Wholesale punching stores end-customer details in orders.extra_data so
    # party/dealer name remains clean. Backoffice is the final correction
    # point before production/card print, so expose the same fields here.
    try:
        import json as _bo_ec_json
        _extra_ec = order.get("extra_data") or {}
        if isinstance(_extra_ec, str):
            try:
                _extra_ec = _bo_ec_json.loads(_extra_ec) or {}
            except Exception:
                _extra_ec = {}
        if not isinstance(_extra_ec, dict):
            _extra_ec = {}
        _end_customer = dict(_extra_ec.get("end_customer") or {})
        _is_wholesale_order = str(order.get("order_type") or "").upper() == "WHOLESALE"
        _auth_name_default = (
            _end_customer.get("name")
            or ("" if _is_wholesale_order else order.get("patient_name"))
            or ""
        )
        _auth_mobile_default = (
            _end_customer.get("mobile")
            or ("" if _is_wholesale_order else order.get("patient_mobile"))
            or ""
        )
        _auth_ref_default = (
            _end_customer.get("ref")
            or order.get("customer_order_no")
            or ""
        )
        with st.expander("🪪 Customer / Authenticity Card Details", expanded=False):
            st.caption("Floats from punching. Used by authenticity card, labels and production print.")
            _ec1, _ec2, _ec3 = st.columns(3)
            _auth_name = _ec1.text_input(
                "Customer name on card",
                value=str(_auth_name_default or ""),
                key=f"bo_auth_name_{order_id}",
            )
            _auth_mobile = _ec2.text_input(
                "Customer mobile",
                value=str(_auth_mobile_default or ""),
                key=f"bo_auth_mobile_{order_id}",
            )
            _auth_ref = _ec3.text_input(
                "Customer order no / ref",
                value=str(_auth_ref_default or ""),
                key=f"bo_auth_ref_{order_id}",
            )
            if st.button("💾 Save customer/card details", key=f"bo_auth_save_{order_id}",
                         type="primary", use_container_width=True):
                try:
                    from modules.sql_adapter import run_write as _rw_auth
                    _extra_new = dict(_extra_ec)
                    _extra_new["end_customer"] = {
                        "name": str(_auth_name or "").strip(),
                        "mobile": str(_auth_mobile or "").strip(),
                        "ref": str(_auth_ref or "").strip(),
                    }
                    _params_auth = {
                        "oid": str(order.get("id") or order.get("order_id") or ""),
                        "co": str(_auth_ref or "").strip(),
                        "ed": _bo_ec_json.dumps(_extra_new),
                    }
                    if _is_wholesale_order:
                        _rw_auth("""
                            UPDATE orders
                            SET customer_order_no=%(co)s,
                                extra_data=%(ed)s::jsonb,
                                updated_at=NOW()
                            WHERE id=%(oid)s::uuid
                        """, _params_auth)
                    else:
                        _params_auth.update({
                            "pn": str(_auth_name or "").strip(),
                            "pm": str(_auth_mobile or "").strip(),
                        })
                        _rw_auth("""
                            UPDATE orders
                            SET patient_name=%(pn)s,
                                patient_mobile=%(pm)s,
                                customer_order_no=%(co)s,
                                extra_data=%(ed)s::jsonb,
                                updated_at=NOW()
                            WHERE id=%(oid)s::uuid
                        """, _params_auth)
                    order["customer_order_no"] = str(_auth_ref or "").strip()
                    order["extra_data"] = _extra_new
                    if not _is_wholesale_order:
                        order["patient_name"] = str(_auth_name or "").strip()
                        order["patient_mobile"] = str(_auth_mobile or "").strip()
                    st.success("✅ Customer/card details saved")
                    try:
                        from . import order_loader as _ol_auth
                        for _fn_name in ("load_single_order", "load_orders_from_database", "load_orders_summary"):
                            _fn = getattr(_ol_auth, _fn_name, None)
                            if _fn is not None and hasattr(_fn, "clear"):
                                _fn.clear()
                    except Exception:
                        pass
                    st.rerun()
                except Exception as _auth_err:
                    st.error(f"Save failed: {_auth_err}")
    except Exception as _auth_panel_err:
        logger.warning("Customer/authenticity panel failed: %s", _auth_panel_err, exc_info=True)

    
    #  NEW: Trigger product change dialog if modal is active
    if st.session_state.get('bo_product_change_modal', {}).get('active', False):
        product_change_dialog()
    
    # Build all_lines BEFORE tabs so every tab can access it
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    all_lines.extend(order.get('service_lines', []))
    # Normalize names on every loaded line; cached service rows from older
    # sessions may still carry "Unknown Product" even though lens_params has
    # the correct service label.
    for _ln_name_fix in all_lines:
        _ln_name_fix["product_name"] = _bo_line_display_name(_ln_name_fix)
        _bo_normalize_line_numbers(_ln_name_fix)
        _bo_enforce_wholesale_price(_ln_name_fix, order.get("order_type"))
    # Defensive hydration: older cached order objects can miss service_lines
    # even when DB has them. Pull service/other rows directly so backoffice
    # totals and display never silently drop charges.
    try:
        from modules.sql_adapter import run_query as _rq_bo_svc_lines, resolve_order_uuid as _resolve_order_uuid
        _oid_bo_svc = _resolve_order_uuid(order.get("id") or order.get("order_id") or order.get("order_no")) or ""
        _seen_lids_bo = {str(l.get("line_id") or l.get("id") or "") for l in all_lines}
        if _oid_bo_svc:
            _svc_db_rows = _rq_bo_svc_lines("""
                SELECT ol.id::text AS line_id, ol.order_id::text AS order_id,
                       ol.product_id::text AS product_id,
                       COALESCE(p.product_name,
                                ol.lens_params->>'service_display_name',
                                ol.lens_params->>'display_product_name',
                                ol.lens_params->>'service_description',
                                'Service') AS product_name,
                       COALESCE(p.brand, 'Services') AS brand,
                       COALESCE(p.main_group, 'Services') AS main_group,
                       COALESCE(p.category, 'Services') AS category,
                       COALESCE(p.unit, 'SERVICE') AS unit,
                       ol.eye_side,
                       COALESCE(ol.quantity, 1) AS billing_qty,
                       COALESCE(ol.allocated_qty, 0) AS allocated_qty,
                       COALESCE(ol.ready_qty, 0) AS ready_qty,
                       COALESCE(ol.billed_qty, 0) AS billed_qty,
                       COALESCE(ol.unit_price, 0)::numeric AS unit_price,
                       COALESCE(ol.billing_total, ol.total_price, 0)::numeric AS billing_total,
                       COALESCE(ol.gst_percent, 0)::numeric AS gst_percent,
                       COALESCE(ol.gst_amount, 0)::numeric AS gst_amount,
                       COALESCE(ol.discount_amount, 0)::numeric AS discount_amount,
                       COALESCE(ol.discount_percent, 0)::numeric AS discount_percent,
                       ol.status,
                       ol.batch_status,
                       ol.lens_params,
                       ol.boxing_params,
                       TRUE AS is_service_line
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id=%(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE)=FALSE
                  AND (
                    COALESCE(ol.is_service_line, FALSE)=TRUE
                    OR UPPER(COALESCE(ol.eye_side,'')) IN ('S','SERVICE')
                  )
            """, {"oid": _oid_bo_svc}) or []
            _line_idx_bo = {
                str(l.get("line_id") or l.get("id") or ""): _i
                for _i, l in enumerate(all_lines)
            }
            for _sr_bo in _svc_db_rows:
                _lid_bo = str(_sr_bo.get("line_id") or "")
                _lp_bo = _sr_bo.get("lens_params") or {}
                _sr_bo["manufacturing_route"] = (
                    _lp_bo.get("manufacturing_route") if isinstance(_lp_bo, dict) else None
                )
                _sr_bo["product_name"] = _bo_line_display_name(dict(_sr_bo))
                _sr_bo["unit"] = str(_sr_bo.get("unit") or "SERVICE").upper()
                _sr_bo["box_size"] = 1
                _bo_normalize_line_numbers(_sr_bo)
                if _lid_bo and _lid_bo not in _seen_lids_bo:
                    all_lines.append(dict(_sr_bo))
                    order.setdefault("service_lines", []).append(dict(_sr_bo))
                    _seen_lids_bo.add(_lid_bo)
                elif _lid_bo in _line_idx_bo:
                    # Service pricing is authoritative from DB. Do not let an
                    # in-memory discount refresh turn ₹60+GST into ₹54.
                    all_lines[_line_idx_bo[_lid_bo]].update(dict(_sr_bo))
    except Exception:
        pass

    # Live line hydration for billing/stage decisions. Production/vendor panels
    # can update supplier_stage, ready_qty, status, and lens_params after the
    # order card was loaded; Backoffice billing must use DB truth here.
    try:
        from modules.sql_adapter import run_query as _rq_bo_live_lines, resolve_order_uuid as _resolve_order_uuid_live
        _oid_bo_live = _resolve_order_uuid_live(order.get("id") or order.get("order_id") or order.get("order_no")) or ""
        if _oid_bo_live:
            _live_rows_bo = _rq_bo_live_lines("""
                SELECT
                    ol.id::text AS line_id,
                    ol.order_id::text AS order_id,
                    ol.product_id::text AS product_id,
                    COALESCE(p.product_name,
                             ol.lens_params->>'display_product_name',
                             ol.lens_params->>'service_display_name',
                             ol.lens_params->>'service_description',
                             'Line') AS product_name,
                    COALESCE(p.brand, '') AS brand,
                    COALESCE(p.main_group, '') AS main_group,
                    COALESCE(p.category, '') AS category,
                    COALESCE(p.unit, '') AS unit,
                    ol.eye_side,
                    COALESCE(ol.quantity, 1) AS billing_qty,
                    COALESCE(ol.quantity, 1) AS quantity,
                    COALESCE(ol.allocated_qty, 0) AS allocated_qty,
                    COALESCE(ol.ready_qty, 0) AS ready_qty,
                    COALESCE(ol.billed_qty, 0) AS billed_qty,
                    COALESCE(ol.unit_price, 0)::numeric AS unit_price,
                    COALESCE(ol.billing_total, ol.total_price, 0)::numeric AS billing_total,
                    COALESCE(ol.total_price, ol.billing_total, 0)::numeric AS total_price,
                    COALESCE(ol.gst_percent, 0)::numeric AS gst_percent,
                    COALESCE(ol.gst_amount, 0)::numeric AS gst_amount,
                    COALESCE(ol.discount_amount, 0)::numeric AS discount_amount,
                    COALESCE(ol.discount_percent, 0)::numeric AS discount_percent,
                    ol.status,
                    ol.batch_status,
                    ol.lens_params,
                    ol.boxing_params,
                    COALESCE(ol.is_service_line, FALSE) AS is_service_line,
                    ol.sph, ol.cyl, ol.axis, ol.add_power,
                    ol.production_ref
                FROM order_lines ol
                LEFT JOIN products p ON p.id = ol.product_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
            """, {"oid": _oid_bo_live}) or []
            _live_idx = {str(l.get("line_id") or l.get("id") or ""): _i for _i, l in enumerate(all_lines)}
            for _lr_bo in _live_rows_bo:
                _lid_live = str(_lr_bo.get("line_id") or "")
                _lp_live = _lr_bo.get("lens_params") or {}
                if isinstance(_lp_live, dict):
                    _lr_bo["manufacturing_route"] = _lp_live.get("manufacturing_route")
                    _lr_bo["supplier_stage"] = (
                        _lp_live.get("supplier_stage")
                        or _lp_live.get("external_lab_stage")
                        or _lr_bo.get("status")
                    )
                _lr_bo["product_name"] = _bo_line_display_name(dict(_lr_bo))
                _bo_normalize_line_numbers(_lr_bo)
                _bo_enforce_wholesale_price(_lr_bo, order.get("order_type"))
                if _lid_live in _live_idx:
                    all_lines[_live_idx[_lid_live]].update(dict(_lr_bo))
                else:
                    all_lines.append(dict(_lr_bo))
    except Exception:
        pass

    # ── RETAIL: always show MRP — runs every render ──────────────────────────
    # For RETAIL orders, unit_price MUST be MRP regardless of what is cached
    # in the line dict. Uses price_qty_governor rule: RETAIL → mrp field.
    # Runs unconditionally (no gate) so every render reflects DB truth.
    _rt_order_type = str(order.get("order_type") or "RETAIL").upper()
    if _rt_order_type == "RETAIL":
        try:
            from modules.sql_adapter import run_query as _rq_rt
            from modules.sql_adapter import run_write as _rw_rt
            from modules.core.price_qty_governor import normalize_to_pcs_price as _norm_rt
            # One batch query for all non-service lines
            _rt_pids = list({
                str(l.get("product_id") or "")
                for l in all_lines
                if l.get("product_id")
                and not l.get("is_service_line")
                and not l.get("manual_price_override")
                and str(l.get("eye_side") or "").upper() not in ("S", "SERVICE")
            })
            if _rt_pids:
                _rt_mrp_rows = _rq_rt("""
                    SELECT
                        i.product_id::text,
                        COALESCE(MAX(NULLIF(i.mrp, 0)), 0)          AS mrp,
                        COALESCE(p.box_size, 1)                     AS box_size,
                        COALESCE(p.gst_percent, 0)                  AS gst_pct
                    FROM inventory_stock i
                    JOIN products p ON p.id = i.product_id
                    WHERE i.product_id = ANY(%(pids)s::uuid[])
                      AND COALESCE(i.is_active, TRUE) = TRUE
                    GROUP BY i.product_id, p.box_size, p.gst_percent
                """, {"pids": _rt_pids}) or []

                _rt_mrp_map = {
                    r["product_id"]: {
                        "mrp": float(r.get("mrp") or 0),
                        "box_size": max(1, int(r.get("box_size") or 1)),
                        "gst_pct":  float(r.get("gst_pct") or 0),
                    }
                    for r in _rt_mrp_rows
                }

                for _rt_line in all_lines:
                    if bool(_rt_line.get("is_service_line")):
                        continue
                    if bool(_rt_line.get("manual_price_override")):
                        continue
                    if str(_rt_line.get("eye_side") or "").upper() in ("S", "SERVICE"):
                        continue
                    _rt_pid = str(_rt_line.get("product_id") or "")
                    _rt_info  = _rt_mrp_map.get(_rt_pid, {})
                    _rt_mrp   = float(_rt_info.get("mrp") or 0)
                    _rt_bsz   = int(_rt_info.get("box_size") or _rt_line.get("box_size") or 1)
                    _rt_gst   = float(_rt_info.get("gst_pct") or _rt_line.get("gst_percent") or 0)
                    _rt_src   = "retail_mrp"

                    # Ophthalmic RX products often have no inventory_stock
                    # price row; their retail price lives in
                    # ophthalmic_lens_specs.srp_per_pair.  Use the final
                    # backoffice/punching lens params to find the exact
                    # index/coating/treatment, then convert pair → per lens.
                    if _rt_mrp <= 0:
                        _rt_lp = _rt_line.get("lens_params") or {}
                        if isinstance(_rt_lp, str):
                            try:
                                import json as _json_rt
                                _rt_lp = _json_rt.loads(_rt_lp or "{}")
                            except Exception:
                                _rt_lp = {}
                        _rt_idx = (
                            _rt_lp.get("lens_index")
                            or _rt_lp.get("index")
                            or _rt_line.get("index_value")
                        )
                        _rt_coat = (
                            _rt_lp.get("coating")
                            or _rt_lp.get("coating_type")
                            or _rt_line.get("coating")
                        )
                        _rt_treat = (
                            _rt_lp.get("treatment")
                            or _rt_line.get("treatment")
                            or "Clear"
                        )
                        if _rt_pid and _rt_idx and _rt_coat:
                            _rt_spec_rows = _rq_rt("""
                                SELECT
                                    COALESCE(srp_per_pair, 0) AS srp_pair,
                                    COALESCE(wlp_per_pair, 0) AS wlp_pair
                                FROM ophthalmic_lens_specs
                                WHERE product_id = %(pid)s::uuid
                                  AND index_value = %(idx)s::numeric
                                  AND coating = %(coat)s
                                  AND COALESCE(treatment, 'Clear') = COALESCE(%(treat)s, 'Clear')
                                  AND COALESCE(is_active, TRUE) = TRUE
                                LIMIT 1
                            """, {
                                "pid": _rt_pid,
                                "idx": str(_rt_idx),
                                "coat": str(_rt_coat),
                                "treat": str(_rt_treat or "Clear"),
                            }) or []
                            if _rt_spec_rows:
                                _rt_pair = float(
                                    _rt_spec_rows[0].get("srp_pair")
                                    or _rt_spec_rows[0].get("wlp_pair")
                                    or 0
                                )
                                if _rt_pair > 0:
                                    _rt_mrp = round(_rt_pair / 2, 2)
                                    _rt_bsz = 1
                                    _rt_src = "retail_oph_srp"
                    if _rt_mrp <= 0:
                        continue
                    # Normalize: if stored as BOX price, convert to PCS
                    _rt_pcs_mrp = round(_rt_mrp / _rt_bsz, 4) if _rt_bsz > 1 else _rt_mrp
                    _rt_qty     = int(_rt_line.get("billing_qty") or _rt_line.get("quantity") or 1)
                    _rt_total   = round(_rt_pcs_mrp * _rt_qty, 2)
                    _rt_prev_total = float(
                        _rt_line.get("billing_total")
                        or _rt_line.get("total_price")
                        or 0
                    )
                    # Always set — RETAIL rule: price = MRP, GST inclusive
                    _rt_line["unit_price"]   = _rt_pcs_mrp
                    _rt_line["billing_total"] = _rt_total
                    _rt_line["total_price"]   = _rt_total
                    _rt_line["tax_inclusive"]  = True
                    _rt_line["gst_percent"]    = _rt_gst
                    _rt_line["price_source"]   = _rt_src
                    _rt_lid = str(_rt_line.get("line_id") or _rt_line.get("id") or "")
                    if _rt_lid and abs(_rt_prev_total - _rt_total) > 0.01:
                        # PRICE WRITE GUARD (retail price-stamp): refuse to
                        # overwrite a good price with 0. The resolver can
                        # legitimately return 0 if no price source matches —
                        # writing that to the DB is exactly the bug we are
                        # blocking. Mirrors from sibling line if possible.
                        _rt_line["unit_price"] = _rt_pcs_mrp
                        _gok_rt, _, _mp_rt, _mg_rt = _guard_line_price_before_write(
                            _rt_line, order, all_lines=all_lines,
                            context="retail-stamp",
                        )
                        if not _gok_rt:
                            # guard already showed error; skip this write
                            # and continue rendering other lines
                            continue
                        if _mp_rt is not None:
                            _rt_pcs_mrp = float(_mp_rt)
                            _rt_total   = round(_rt_pcs_mrp * float(_rt_line.get("quantity") or 1), 2)
                            if _mg_rt is not None:
                                _rt_gst = float(_mg_rt)
                        try:
                            _rw_rt("""
                                UPDATE order_lines
                                SET unit_price=%(up)s,
                                    total_price=%(tp)s,
                                    billing_total=%(tp)s,
                                    gst_percent=%(gp)s
                                WHERE id=%(lid)s::uuid
                            """, {
                                "up": _rt_pcs_mrp,
                                "tp": _rt_total,
                                "gp": _rt_gst,
                                "lid": _rt_lid,
                            })
                            _bo_refresh_order_total_value(str(order.get("id") or order_id))
                        except Exception:
                            pass
        except Exception:
            pass   # never block render

    # Price-source hard guard for stale session orders. The DB loader already
    # returns corrected wholesale prices, but an order opened before a reload can
    # remain in st.session_state with old MRP/retail unit_price. Re-stamp lines
    # here before any Order Summary or Billing Summary math.
    _order_type_guard = str(order.get("order_type") or "RETAIL").upper()
    for _ln_price_guard in all_lines:
        try:
            _ln_price_guard["order_type"] = _order_type_guard
            # Explicit guard — only apply wholesale price enforcement to WHOLESALE orders
            if _order_type_guard != "WHOLESALE" \
                    or bool(_ln_price_guard.get("is_service_line")) \
                    or str(_ln_price_guard.get("order_type") or "").upper() == "RETAIL":
                continue
            _pid_guard = str(_ln_price_guard.get("product_id") or "")
            if not _pid_guard:
                continue
            _lp_guard = _ln_price_guard.get("lens_params") or {}
            if isinstance(_lp_guard, str):
                import json as _json_pg
                try:
                    _lp_guard = _json_pg.loads(_lp_guard)
                except Exception:
                    _lp_guard = {}
            _manual_price_guard = (
                "manual" in str(_ln_price_guard.get("price_source") or "").lower()
                or bool(_ln_price_guard.get("manual_price_override"))
                or bool((_lp_guard or {}).get("manual_price_override"))
                or bool((_lp_guard or {}).get("price_locked"))
            )
            if _manual_price_guard:
                continue
            from modules.core.price_source_resolver import resolve_db_price as _resolve_db_price_bo
            from modules.core.price_qty_governor import compute_line_gst as _compute_line_gst_bo

            _resolved_bo = _resolve_db_price_bo(
                _pid_guard,
                "WHOLESALE",
                product=_ln_price_guard,
                prefer_batch=True,
            )
            _pcs_bo = float(_resolved_bo.get("pcs_price") or 0)
            _cur_bo = float(_ln_price_guard.get("unit_price") or 0)
            if _pcs_bo > 0 and (_cur_bo <= 0 or abs(_cur_bo - _pcs_bo) > 0.5):
                _qty_bo = int(_ln_price_guard.get("billing_qty") or _ln_price_guard.get("quantity") or 0)
                _disc_pc_bo = float(_ln_price_guard.get("discount_percent") or 0)
                _gross_bo = round(_pcs_bo * _qty_bo, 2)
                _disc_bo = round(_gross_bo * _disc_pc_bo / 100, 2) if _disc_pc_bo > 0 else float(_ln_price_guard.get("discount_amount") or 0)
                _net_bo = round(max(0.0, _gross_bo - _disc_bo), 2)
                _gst_bo = _compute_line_gst(
                    _net_bo / max(_qty_bo, 1),
                    _qty_bo,
                    float(_ln_price_guard.get("gst_percent") or 0),
                    "WHOLESALE",
                )
                _ln_price_guard["unit_price"] = _pcs_bo
                _ln_price_guard["billing_total"] = _gst_bo["subtotal"]
                _ln_price_guard["total_price"] = _gst_bo["subtotal"]
                _ln_price_guard["gst_amount"] = _gst_bo["gst_amount"]
                _ln_price_guard["discount_amount"] = _disc_bo
                _ln_price_guard["price_source"] = str(_resolved_bo.get("source") or "price_guard")
                _ln_price_guard["tax_inclusive"] = False
        except Exception as _pg_e:
            # FIX: previously `except Exception: pass` — that is exactly how
            # this whole class of "saved with unit_price=0" bug stays hidden.
            # Log loudly. Caller continues to next line (one broken line must
            # not break the whole render), but ops can see the failure.
            import logging, traceback
            logging.getLogger(__name__).warning(
                "[wholesale price-guard] failed on line "
                "product_id=%s eye=%s : %s\n%s",
                _ln_price_guard.get("product_id"),
                _ln_price_guard.get("eye_side"),
                _pg_e, traceback.format_exc(),
            )
            pass

    # ═══════════════════════════════════════════════════════════════════════
    # Auto-refresh discount + run validators on order load
    # ═══════════════════════════════════════════════════════════════════════
    # Why this exists: previously the discount engine only fired inside
    # product_change_dialog, and validators were never run in backoffice at
    # all. That meant:
    #   - Discounts showed stale numbers when an order was reopened
    #   - Staff didn't see validation issues until billing / save
    # Both refresh here on every order load so the displayed totals and any
    # validation issues are current.
    #
    # We gate by a session key so we don't re-fire on every Streamlit rerun
    # within the same order — only when the order_id changes or staff
    # explicitly clicks "Refresh checks".
    _bo_validation_cache_version = "retail_service_gst_validation_20260522"
    _bo_checks_key = f"_bo_disc_val_done_{order_id}_{_bo_validation_cache_version}"
    _bo_legacy_checks_key = f"_bo_disc_val_done_{order_id}"
    _bo_legacy_val_key = f"_bo_val_result_{order_id}"
    if st.session_state.get(_bo_legacy_checks_key) and not st.session_state.get(_bo_checks_key):
        st.session_state.pop(_bo_legacy_checks_key, None)
        st.session_state.pop(_bo_legacy_val_key, None)
    _bo_force_recheck = st.session_state.pop("_bo_force_recheck", False)
    if _bo_force_recheck or not st.session_state.get(_bo_checks_key):
        # Resolve party_id with fallback (same pattern as product_change_dialog)
        _bo_party_id = str(order.get("party_id") or "").strip()
        if not _bo_party_id:
            _party_name_bo = str(
                order.get("party_name") or order.get("patient_name") or ""
            ).strip()
            if _party_name_bo:
                try:
                    from modules.sql_adapter import run_query as _rq_disc
                    _pr = _rq_disc(
                        "SELECT id::text AS id FROM parties "
                        "WHERE party_name = %(n)s AND COALESCE(is_active,TRUE) = TRUE "
                        "LIMIT 1",
                        {"n": _party_name_bo},
                    ) or []
                    if _pr:
                        _bo_party_id = str(_pr[0].get("id") or "")
                except Exception:
                    pass
        _bo_order_type = str(order.get("order_type") or "RETAIL").upper()

        # --- Discount refresh ---
        try:
            from modules.pricing.discount_flow import apply_order_discounts
            # Only re-apply to non-service, non-locked lines. Manual price
            # overrides and price-locked lines should NOT be re-discounted.
            _disc_lines = [
                l for l in all_lines
                if not bool(l.get("is_service_line"))
                and not bool(l.get("manual_price_override"))
                and not bool((l.get("lens_params") or {}).get("price_locked")
                             if isinstance(l.get("lens_params"), dict) else False)
            ]
            if _disc_lines:
                apply_order_discounts(
                    _disc_lines,
                    party_id=_bo_party_id,
                    order_type=_bo_order_type,
                )
        except Exception as _de:
            import logging
            logging.getLogger(__name__).warning(
                f"[backoffice] discount refresh failed: {_de}"
            )

        # --- Validator run ---
        _bo_validation_result = None
        try:
            from modules.validation_gateway import validate_before_submit
            # Validator requires order_id, party_name, lines. Existing order
            # dict uses different keys depending on origin (id / order_id /
            # order_no). Build a normalized payload.
            _resolved_oid = (
                order.get("order_id")
                or order.get("id")
                or order.get("order_no")
                or order_id
            )
            _val_order = dict(order)
            _val_order["order_id"]   = str(_resolved_oid or "")
            _val_order["lines"]      = all_lines
            _val_order["party_name"] = str(
                order.get("party_name")
                or order.get("patient_name")
                or order.get("customer_name")
                or order.get("order_no")
                or "Customer"
            )
            # PartyValidator reads the canonical key "party".  Wholesale
            # orders often arrive with party_name only, so mirror the resolved
            # value there instead of showing a false "Party name missing" error.
            _val_order["party"] = _val_order["party_name"]
            _bo_validation_result = validate_before_submit(_val_order)
        except Exception as _ve:
            import logging
            logging.getLogger(__name__).warning(
                f"[backoffice] validator run failed: {_ve}"
            )
            _bo_validation_result = None

        # Stash result for the floating panel below + mark done
        st.session_state[f"_bo_val_result_{order_id}"] = _bo_validation_result
        st.session_state[_bo_checks_key] = True

    # Render floating validator/discount status panel (above tabs)
    _val_res = st.session_state.get(f"_bo_val_result_{order_id}")
    # FIX: exclude soft-deleted lines from the displayed discount sum (same
    # class of bug as the Billing Verification / Order Summary totals).
    _disc_total_show = sum(
        float(l.get("discount_amount") or 0) for l in all_lines if not bool(l.get("is_deleted"))
    )
    with st.container():
        _vc1, _vc2, _vc3 = st.columns([5, 2, 2])
        with _vc1:
            if _val_res and isinstance(_val_res, dict):
                _errs = _val_res.get("errors") or []
                _warns = _val_res.get("warnings") or []
                if _errs:
                    st.error(
                        "⛔ **Validation errors (" + str(len(_errs)) + "):**\n\n"
                        + "\n".join("- " + (e.get("message") if isinstance(e, dict) else str(e))
                                    for e in _errs[:6])
                        + ("\n- ...and more" if len(_errs) > 6 else "")
                    )
                if _warns:
                    st.warning(
                        "⚠️ **Warnings (" + str(len(_warns)) + "):**\n\n"
                        + "\n".join("- " + (w.get("message") if isinstance(w, dict) else str(w))
                                    for w in _warns[:6])
                        + ("\n- ...and more" if len(_warns) > 6 else "")
                    )
                if not _errs and not _warns:
                    st.success("✅ All validators passed.")
            else:
                st.caption("Validators: not run for this order yet.")
        with _vc2:
            st.metric(
                "Discount",
                f"₹{_disc_total_show:,.2f}",
                help="Auto-refreshed from discount engine on order load.",
            )
        with _vc3:
            if st.button(
                "🔄 Refresh checks",
                key=f"bo_force_recheck_{order_id}",
                use_container_width=True,
                help="Re-runs discount engine and validators for this order.",
            ):
                st.session_state.pop(_bo_checks_key, None)
                st.session_state["_bo_force_recheck"] = True
                st.rerun()
    st.divider()
    # ═══════════════════════════════════════════════════════════════════════

    # Tabs for different sections
    # Auto-jump to billing tab if coming from production page OR after challan creation
    _jump_billing = (
        st.session_state.pop("bo_jump_to_billing", False)
        or st.session_state.pop("bo_show_billing_tab", False)
    )

    # When jumped from production page, highlight the billing tab path
    if _jump_billing:
        st.info(
            "💰 **Navigated from Production page** — "
            "open the **Billing Summary** tab below to create challan.",
            icon="💰"
        )

    _order_status_gate = str(order.get("status") or "").upper()
    if _order_status_gate in ("HOLD", "CREDIT_HOLD", "PENDING_PAYMENT"):
        st.markdown(
            "<div style='background:#1a0a00;border:1px solid #f97316;"
            "border-radius:8px;padding:12px 16px;margin:8px 0'>"
            "<span style='color:#fb923c;font-weight:800'>⏸ Order is on hold</span>"
            "<span style='display:block;color:#94a3b8;font-size:.82rem;margin-top:4px'>"
            "Assignment, editing, challan, invoice and production movement are blocked. "
            "Release the hold first, then reopen and save/confirm if needed.</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        try:
            from modules.backoffice.guidance import render_hold_confirm_panel
            _hold_oid = str(order.get("id") or order.get("order_id") or "")
            _hold_ono = str(order.get("order_no") or "")
            render_hold_confirm_panel(_hold_oid, _hold_ono, _order_status_gate)
        except Exception as _hold_ex:
            st.error(f"Hold panel unavailable: {_hold_ex}")
        return

    # Streamlit tabs render every tab body on every rerun, which made Backoffice
    # detail sticky because Order Items + Status + Billing + Dispatch all hit DB.
    # Use a section selector and render only the chosen body.
    _bo_section_key = f"bo_detail_section_{order_id}"
    _bo_sections = ["📦 Order Items", "📊 Status", "💰 Billing Summary", "🚀 Dispatch"]
    if _jump_billing:
        st.session_state[_bo_section_key] = "💰 Billing Summary"
    if st.session_state.get(_bo_section_key) not in _bo_sections:
        st.session_state[_bo_section_key] = "📦 Order Items"
    _bo_active_section = st.radio(
        "Backoffice section",
        _bo_sections,
        key=_bo_section_key,
        horizontal=True,
        label_visibility="collapsed",
    )
    tab1 = tab3 = tab4 = tab7 = st.container()
    
    # Consume the one-shot print-suppression guard now that Backoffice has loaded.
    st.session_state.pop("_navigating_to_billing", None)

    if _bo_active_section == "📦 Order Items":
        with tab1:
            st.markdown("###  Order Line Items")
        
            #  Sort lines - Right eye before Left eye
            def eye_sort_key(line):
                eye = line.get('eye_side', '').upper()
                if eye == 'RIGHT' or eye == 'R':
                    return 0
                elif eye == 'LEFT' or eye == 'L':
                    return 1
                else:
                    return 2
        
            all_lines.sort(key=eye_sort_key)
        
            # ── Read-only notice when order is confirmed / in pipeline ────────────
            # Compute frozen state early so it can gate edit buttons below.
            # CONFIRMED is intentionally NOT in this set: after a production rollback
            # the order returns to CONFIRMED so backoffice staff can re-edit.
            # Only statuses that indicate active pipeline work or billing lock the UI.
            _order_status_upper_t1 = str(order.get("status","")).upper()
            _pipeline_locked_t1 = {
                "IN_PRODUCTION","READY","CHALLANED","INVOICED",
                "DISPATCHED","DELIVERED","CLOSED",
            }
            _has_challan_t1 = False
            try:
                # WIN 2: cache challan result in session_state for this order+render
                # so the second identical check (save gate, line ~4550) costs 0 DB hits.
                # Cache is cleared in the save block and after any challan action.
                _ch_cache_key = f"_bo_challan_exists_{order_id}"
                if _ch_cache_key not in st.session_state:
                    from modules.sql_adapter import run_query as _rq_t1
                    _ch_t1 = _rq_t1("""
                        SELECT 1 FROM challans c
                        WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                            OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                          AND c.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1
                    """, {"oid": str(order.get("id") or ""), "ono": str(order.get("order_no") or "")})
                    st.session_state[_ch_cache_key] = bool(_ch_t1)
                _has_challan_t1 = st.session_state[_ch_cache_key]
            except Exception:
                pass
            _non_svc_t1 = [l for l in all_lines
                           if not l.get("is_service_line")
                           and str(l.get("eye_side","")).upper() not in ("S","SERVICE")]
            _has_supplier_sent_t1 = False
            try:
                for _lck_line in _non_svc_t1:
                    _lck_lp = _lck_line.get("lens_params") or {}
                    if isinstance(_lck_lp, str):
                        try:
                            _lck_lp = json.loads(_lck_lp)
                        except Exception:
                            _lck_lp = {}
                    if not isinstance(_lck_lp, dict):
                        _lck_lp = {}
                    _lck_route = str(
                        _lck_line.get("manufacturing_route")
                        or _lck_lp.get("manufacturing_route")
                        or ""
                    ).upper()
                    _lck_supplier_stage = str(
                        _lck_line.get("supplier_stage")
                        or _lck_lp.get("supplier_stage")
                        or _lck_lp.get("external_lab_stage")
                        or ""
                    ).upper()
                    _lck_repl_status = str(_lck_lp.get("replenishment_status") or "").upper()
                    _lck_ref = str(
                        _lck_lp.get("supplier_order_no")
                        or _lck_lp.get("supplier_confirmation_no")
                        or _lck_lp.get("replenishment_po_no")
                        or _lck_line.get("supplier_order_id")
                        or ""
                    ).strip()
                    _real_supplier_progress = (
                        bool(_lck_ref)
                        or _lck_supplier_stage in (
                            "SUPPLIER_CONFIRMED", "AWAITING_SUPPLY",
                            "RECEIVED", "INSPECTION", "READY_FOR_BILLING",
                        )
                    )
                    if _lck_route in ("VENDOR", "EXTERNAL_LAB") and _real_supplier_progress:
                        _has_supplier_sent_t1 = True
                        break
                    if _lck_route == "STOCK" and _lck_repl_status in ("PO_SENT", "ORDERED", "PROCURED", "RECEIVED"):
                        _has_supplier_sent_t1 = True
                        break
            except Exception:
                _has_supplier_sent_t1 = False
            _tab1_edit_locked = (
                len(_non_svc_t1) > 0
                and (
                    _order_status_upper_t1 in _pipeline_locked_t1
                    or _has_challan_t1
                    or _has_supplier_sent_t1
                )
            )

            if _tab1_edit_locked:
                if _has_challan_t1:
                    _lock_reason = "a challan has been raised for this order"
                    _lock_action = "Cancel the challan from the Billing Summary tab first."
                elif _has_supplier_sent_t1:
                    _lock_reason = "one or more items have already been sent to supplier / external lab"
                    _lock_action = "Use the production set-back flow before changing product, power, or quantity."
                else:
                    _lock_reason = f"order status is **{_order_status_upper_t1}**"
                    _lock_action = "Use Supervisor Override for power corrections if needed."
                st.markdown(
                    f"<div style='background:#0a1a0a;border:1px solid #166534;"
                    f"border-left:4px solid #22c55e;border-radius:8px;"
                    f"padding:10px 16px;margin-bottom:12px'>"
                    f"<div style='color:#86efac;font-weight:800;font-size:0.88rem'>"
                    f"✅ Order confirmed — view only</div>"
                    f"<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
                    f"Product, power, and quantity edits are <b style='color:#fbbf24'>not available</b> "
                    f"because {_lock_reason}. {_lock_action}"
                    f"</div>"
                    f"<div style='color:#64748b;font-size:0.72rem;margin-top:6px'>"
                    f"✅ Payment collection &nbsp;·&nbsp; 💬 WhatsApp &nbsp;·&nbsp; "
                    f"🖨️ Print receipt &nbsp;·&nbsp; 💳 Payment link — all available in tabs above."
                    f"</div></div>",
                    unsafe_allow_html=True,
                )

            #  Group by product - R and L together
            product_groups = {}
        
            for idx, line in enumerate(all_lines):
                eye = line.get('eye_side', '').upper()
                if eye not in ['RIGHT', 'R', 'LEFT', 'L']:
                    continue

                pair_id = (
                    line.get('pair_id')
                    or f"{get_display_order_id(order)}_{line.get('product_id','')}"
                )


                if pair_id not in product_groups:
                    product_groups[pair_id] = {
                        'product_name': line.get('product_name', 'N/A'),
                        'brand': line.get('brand', 'N/A'),
                        'R': None,
                        'L': None,
                        'R_idx': None,
                        'L_idx': None
                    }

                if eye in ['RIGHT', 'R']:
                    product_groups[pair_id]['R'] = line
                    product_groups[pair_id]['R_idx'] = idx

                elif eye in ['LEFT', 'L']:
                    product_groups[pair_id]['L'] = line
                    product_groups[pair_id]['L_idx'] = idx

            # Display each product with R and L side by side
            for product_id, group in product_groups.items():
                # Compact product header - just sync option, no big header
                render_product_sync_option(group, product_id)
            
                # ==================== RIGHT & LEFT EYE DYNAMIC LAYOUT ====================
                #  FIX: Dynamic column layout - stretch to fill horizontal space
                has_right = bool(group['R'])
                has_left = bool(group['L'])
            
                # Create columns only for eyes that have data
                if has_right and has_left:
                    # Both eyes ordered - use 2 columns
                    col_r, col_l = st.columns(2)
                elif has_right:
                    # Only right eye - use full width
                    col_r = st.container()
                    col_l = None
                elif has_left:
                    # Only left eye - use full width
                    col_r = None
                    col_l = st.container()
                else:
                    # Neither eye ordered
                    col_r = None
                    col_l = None
            
                # ── WIN 4: pre-fetch mrp / selling_price / stock_qty / supplier ──────
                # Replaces one SELECT per line inside _render_eye_block_ui with one
                # batch query for all product_ids on this order. The closure below
                # reads from _live_price_cache instead of hitting the DB per line.
                _live_price_cache = {}
                try:
                    from modules.sql_adapter import run_query as _rq_lpc
                    _lpc_pids = list({
                        str(l.get("product_id") or "")
                        for l in all_lines
                        if l.get("product_id") and not bool(l.get("is_deleted"))
                    })
                    if _lpc_pids:
                        _lpc_rows = _rq_lpc(
                            """
                            SELECT
                                p.id::text                              AS product_id,
                                COALESCE(MAX(NULLIF(i.mrp,0)), 0)       AS mrp,
                                COALESCE(MAX(NULLIF(i.selling_price,0)), 0) AS selling_price,
                                COALESCE(SUM(COALESCE(i.quantity, 0)), 0)   AS stock_qty,
                                COALESCE(
                                  (SELECT pt.party_name
                                   FROM product_supplier_map psm
                                   JOIN parties pt ON pt.id = psm.supplier_id
                                   WHERE psm.product_id = p.id
                                     AND COALESCE(pt.is_active, TRUE) = TRUE
                                   ORDER BY psm.created_at DESC LIMIT 1),
                                  (SELECT pt.party_name FROM parties pt
                                   WHERE pt.id = p.preferred_supplier_id LIMIT 1),
                                  ''
                                ) AS supplier
                            FROM products p
                            LEFT JOIN inventory_stock i
                                   ON i.product_id = p.id
                                  AND COALESCE(i.is_active, TRUE) = TRUE
                            WHERE p.id = ANY(%(pids)s::uuid[])
                            GROUP BY p.id
                            """,
                            {"pids": _lpc_pids},
                        ) or []
                        _live_price_cache = {r["product_id"]: r for r in _lpc_rows}
                except Exception:
                    _live_price_cache = {}

                # ── Smart compact eye block renderer ──────────────────────
                def _render_eye_block_ui(line, idx, eye_label):
                    import math as _math, json as _ej

                    bdr_color = "#ef4444" if eye_label=="R" else "#64748b"
                    eye_title = "RE — Right Eye" if eye_label=="R" else "LE — Left Eye"

                    # Build power string
                    _pw_parts = []
                    try:
                        if line.get("sph") is not None: _pw_parts.append(f"SPH {float(line['sph']):+.2f}")
                        _c = line.get("cyl")
                        if _c is not None and abs(float(_c or 0)) > 0.01: _pw_parts.append(f"CYL {float(_c):+.2f}")
                        _ax = line.get("axis")
                        if _ax and int(float(_ax or 0)): _pw_parts.append(f"AX {int(float(_ax))}°")
                        _ad = line.get("add_power")
                        if _ad and float(_ad or 0) > 0: _pw_parts.append(f"ADD {float(_ad):+.2f}")
                    except Exception as _e:
                        logger.warning("Suppressed error: %s", _e)
                    _pw_str = "  ".join(_pw_parts) if _pw_parts else "—"

                    # Build lens params summary
                    _lp_e = line.get("lens_params") or {}
                    if isinstance(_lp_e, str):
                        try: _lp_e = _ej.loads(_lp_e)
                        except Exception as _e:
                            logger.warning("Suppressed error: %s", _e)
                            _lp_e = {}
                    _coat = str(_lp_e.get("coating") or _lp_e.get("coating_type") or "").strip()
                    _idx  = str(_lp_e.get("lens_index") or _lp_e.get("index_value") or "").strip()
                    _dia  = str(_lp_e.get("diameter") or "").strip()
                    _frm  = str(_lp_e.get("frame_type") or "").strip()
                    _chips = "  ·  ".join(x for x in [_idx, _coat, _dia, _frm] if x)
                    _prod_ref = str(line.get("production_ref") or "").strip()
                    _order_no_for_ref = str(order.get("order_no") or "").strip()
                    _prod_ref_html = (
                        f"<div style='font-size:0.64rem;color:#fbbf24;margin-bottom:4px'>"
                        f"Ref: {_prod_ref}</div>"
                        if _prod_ref and _prod_ref != _order_no_for_ref else ""
                    )

                    # Qty / allocation
                    _qty     = int(line.get("billing_qty") or line.get("quantity") or 1)
                    _alloc   = int(line.get("allocated_qty") or 0)
                    _to_ord  = max(0, _qty - _alloc)
                    _route_e = str(line.get("manufacturing_route") or _lp_e.get("manufacturing_route") or "").upper()
                    if _route_e in ("VENDOR", "EXTERNAL_LAB"):
                        _alloc_c = "#38bdf8"
                        _sup_nm = str(_lp_e.get("supplier_name") or _lp_e.get("external_lab_name") or "").strip()
                        _alloc_s = f"🏭 Supplier: {_sup_nm}" if _sup_nm else "🏭 Supplier route"
                    elif _route_e == "INHOUSE":
                        _alloc_c = "#a78bfa"
                        _alloc_s = "🔬 In-house production"
                    elif _route_e == "FITTING":
                        _alloc_c = "#f59e0b"
                        _alloc_s = "🔧 Fitting service"
                    elif _route_e == "SERVICE":
                        _alloc_c = "#22c55e"
                        _alloc_s = "✅ Direct service"
                    else:
                        _alloc_c = "#10b981" if _alloc >= _qty else ("#f59e0b" if _alloc > 0 else "#ef4444")
                        _alloc_s = f"✅ {_alloc}/{_qty} allocated" if _alloc >= _qty else                            f"⚡ {_alloc}/{_qty} partial" if _alloc > 0 else f"⬜ Not allocated"

                    # Fetch live pricing + stock + supplier — WIN 4: use batch pre-fetch cache
                    _pid_live = str(line.get("product_id") or "").strip()
                    _mrp_live = _sp_live = _stock_live = 0.0
                    _supplier_live = ""
                    _is_stock_item = any(k in str(line.get("main_group","")).lower()
                                         for k in ("contact","frame","sunglass","accessory","stock"))
                    _order_type_live = str(order.get("order_type") or "RETAIL").upper()
                    if _pid_live:
                        try:
                            _cached_lp = _live_price_cache.get(_pid_live)
                            if _cached_lp:
                                _mrp_live      = float(_cached_lp.get("mrp") or 0)
                                _sp_live       = float(_cached_lp.get("selling_price") or 0)
                                _stock_live    = float(_cached_lp.get("stock_qty") or 0)
                                _supplier_live = str(_cached_lp.get("supplier") or "")
                            else:
                                from modules.sql_adapter import run_query as _rq_live
                                _price_row = _rq_live(
                                    """SELECT
                                          COALESCE(MAX(NULLIF(i.mrp,0)), 0) AS mrp,
                                          COALESCE(MAX(NULLIF(i.selling_price,0)), 0) AS selling_price,
                                          COALESCE(SUM(COALESCE(i.quantity, 0)),0) AS stock_qty,
                                          COALESCE(
                                            (SELECT pt.party_name
                                             FROM product_supplier_map psm
                                             JOIN parties pt ON pt.id = psm.supplier_id
                                             WHERE psm.product_id = p.id
                                               AND COALESCE(pt.is_active,TRUE)=TRUE
                                             ORDER BY psm.created_at DESC
                                             LIMIT 1),
                                            (SELECT pt.party_name
                                             FROM parties pt
                                             WHERE pt.id = p.preferred_supplier_id
                                             LIMIT 1),
                                            ''
                                          ) AS supplier
                                   FROM products p
                                   LEFT JOIN inventory_stock i ON i.product_id = p.id
                                     AND COALESCE(i.is_active,TRUE)=TRUE
                                   WHERE p.id = %(pid)s::uuid
                                   GROUP BY p.id""",
                                    {"pid": _pid_live}
                                )
                                if _price_row:
                                    _mrp_live      = float(_price_row[0].get("mrp") or 0)
                                    _sp_live       = float(_price_row[0].get("selling_price") or 0)
                                    _stock_live    = float(_price_row[0].get("stock_qty") or 0)
                                    _supplier_live = str(_price_row[0].get("supplier") or "")
                        except Exception:
                            pass

                    # Build price info for black box
                    _price_html = ""
                    if _mrp_live > 0:
                        _price_html += f"<span style='color:#94a3b8;font-size:0.65rem'>MRP </span>"                                    f"<span style='color:#f1f5f9;font-size:0.72rem;font-weight:700'>₹{_mrp_live:,.0f}</span>  "
                    if _order_type_live == "WHOLESALE" and _sp_live > 0:
                        _price_html += f"<span style='color:#94a3b8;font-size:0.65rem'>WS </span>"                                    f"<span style='color:#a78bfa;font-size:0.72rem;font-weight:700'>₹{_sp_live:,.0f}</span>  "
                    if _is_stock_item and _stock_live >= 0:
                        _stk_c = "#10b981" if _stock_live > 0 else "#ef4444"
                        _price_html += f"<span style='color:#94a3b8;font-size:0.65rem'>Stock </span>"                                    f"<span style='color:{_stk_c};font-size:0.72rem;font-weight:700'>{int(_stock_live)}</span>  "
                    if _supplier_live:
                        _price_html += f"<span style='color:#94a3b8;font-size:0.65rem'>Supplier </span>"                                    f"<span style='color:#fbbf24;font-size:0.7rem'>{_supplier_live}</span>"

                    # ONE compact HTML card
                    st.markdown(f"""
    <div style='border-top:3px solid {bdr_color};border-radius:0 0 6px 6px;
                padding:8px 10px;background:#0f172a;margin-bottom:2px'>
      <div style='font-size:0.7rem;font-weight:800;color:{bdr_color};
                  letter-spacing:.08em;margin-bottom:4px'>{eye_title}</div>
      <div style='font-size:0.8rem;font-weight:700;color:#f1f5f9;margin-bottom:1px'>
        {line.get("product_name","N/A")}</div>
      <div style='font-size:0.67rem;color:#64748b;margin-bottom:5px'>
        {line.get("brand","N/A")} · {line.get("main_group","N/A")} · {line.get("unit","PCS")}</div>
      {_prod_ref_html}
      <div style='font-size:0.75rem;font-family:monospace;color:#7dd3fc;
                  background:#0c1a3a;padding:3px 8px;border-radius:4px;margin-bottom:4px'
        >{_pw_str}</div>
      {'<div style="font-size:0.67rem;color:#94a3b8;margin-bottom:4px">'+_chips+'</div>' if _chips else ''}
      {'<div style="margin-bottom:4px;padding:3px 0;border-top:1px solid #1e293b">'+_price_html+'</div>' if _price_html else ''}
      <div style='display:flex;justify-content:space-between;align-items:center;
                  font-size:0.7rem;margin-top:2px'>
        <span style='color:{_alloc_c}'>{_alloc_s}</span>
        <span style='color:#64748b;background:#1e293b;padding:1px 6px;border-radius:4px'>
          Qty: {_qty}</span>
      </div>
      {'<div style="color:#f59e0b;font-size:0.67rem;margin-top:2px">⚠️ '+str(_to_ord)+' to order</div>' if (_to_ord > 0 and _route_e not in ("VENDOR","EXTERNAL_LAB","INHOUSE","FITTING","SERVICE")) else ''}
    </div>""", unsafe_allow_html=True)

                    # ── 3 action buttons — hidden when order is confirmed/in-pipeline ─
                    if not _tab1_edit_locked:
                        _ab1, _ab2, _ab3, _ab4 = st.columns(4)
                        with _ab1:
                            if st.button("✏️ Product", key=f"chg_{eye_label}_{idx}",
                                         use_container_width=True, help="Change product"):
                                st.session_state['bo_product_change_modal'] = {
                                    'active': True, 'line': line,
                                    'idx': idx, 'eye_label': eye_label, 'order': order
                                }
                                st.rerun()
                        with _ab2:
                            _pw_key = f"_bo_pwedit_{eye_label}_{idx}"
                            if st.button("🔭 Power", key=f"pw_{eye_label}_{idx}",
                                         use_container_width=True, help="Edit prescription power"):
                                st.session_state[_pw_key] = not st.session_state.get(_pw_key, False)
                        with _ab3:
                            _lp_key = f"_bo_lpedit_{eye_label}_{idx}"
                            if st.button("🔧 Lens Params", key=f"lp_{eye_label}_{idx}",
                                         use_container_width=True, help="Edit index, coating, frame etc"):
                                st.session_state[_lp_key] = not st.session_state.get(_lp_key, False)
                        with _ab4:
                            _bp_key = f"_bo_bpedit_{eye_label}_{idx}"
                            if st.button("📐 Boxing", key=f"bp_{eye_label}_{idx}",
                                         use_container_width=True, help="Edit frame / boxing measurements"):
                                st.session_state[_bp_key] = not st.session_state.get(_bp_key, False)

                    if _tab1_edit_locked:
                        return

                    # ── Power edit panel (inline toggle) ─────────────────
                    if st.session_state.get(f"_bo_pwedit_{eye_label}_{idx}"):
                        try:
                            from modules.backoffice.backoffice_panels import render_power_edit_ui
                            render_power_edit_ui(line, idx, order)
                        except Exception as _pwe:
                            st.error(f"Power edit error: {_pwe}")

                    # ── Lens Parameters edit (inline toggle) ─────────────
                    if st.session_state.get(f"_bo_lpedit_{eye_label}_{idx}"):
                        import json as _lpj
                        _lp_cur = line.get("lens_params") or {}
                        if isinstance(_lp_cur, str):
                            try: _lp_cur = _lpj.loads(_lp_cur)
                            except Exception as _e:
                                logger.warning("Suppressed error: %s", _e)
                                _lp_cur = {}
                        st.markdown("<div style='background:#0c1a2e;border-radius:6px;padding:8px;margin:4px 0'>",
                                    unsafe_allow_html=True)
                        _lc1, _lc2, _lc3 = st.columns(3)
                        _new_idx   = _lc1.text_input("Index",
                            value=str(_lp_cur.get("lens_index") or _lp_cur.get("index_value") or ""),
                            key=f"lp_idx_{eye_label}_{idx}")
                        _new_coat  = _lc2.text_input("Coating",
                            value=str(_lp_cur.get("coating") or _lp_cur.get("coating_type") or ""),
                            key=f"lp_coat_{eye_label}_{idx}")
                        _new_dia   = _lc3.text_input("Diameter",
                            value=str(_lp_cur.get("diameter") or ""),
                            key=f"lp_dia_{eye_label}_{idx}")
                        _lc4, _lc5, _lc6 = st.columns(3)
                        _new_frm   = _lc4.text_input("Frame Type",
                            value=str(_lp_cur.get("frame_type") or ""),
                            key=f"lp_frm_{eye_label}_{idx}")
                        _new_fh    = _lc5.text_input("Fitting Height",
                            value=str(_lp_cur.get("fitting_height") or ""),
                            key=f"lp_fh_{eye_label}_{idx}")
                        _new_corr  = _lc6.text_input("Corridor",
                            value=str(_lp_cur.get("corridor") or ""),
                            key=f"lp_corr_{eye_label}_{idx}")
                        _new_note  = st.text_input("Instructions / Note",
                            value=str(_lp_cur.get("instructions") or ""),
                            key=f"lp_note_{eye_label}_{idx}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        if st.button("💾 Save Lens Params", key=f"lp_save_{eye_label}_{idx}",
                                     type="primary", use_container_width=True):
                            _lp_cur.update({
                                "lens_index": _new_idx, "index_value": _new_idx,
                                "coating": _new_coat, "coating_type": _new_coat,
                                "diameter": _new_dia, "frame_type": _new_frm,
                                "fitting_height": _new_fh, "corridor": _new_corr,
                                "instructions": _new_note,
                            })
                            line["lens_params"] = _lp_cur
                            try:
                                from modules.sql_adapter import run_write as _rw_lp
                                import json as _lpj2
                                _rw_lp("UPDATE order_lines SET lens_params=%(lp)s::jsonb WHERE id=%(lid)s::uuid",
                                       {"lp": _lpj2.dumps(_lp_cur),
                                        "lid": str(line.get("line_id") or line.get("id") or "")})
                                st.success("✅ Lens params saved")
                                st.session_state[f"_bo_lpedit_{eye_label}_{idx}"] = False
                                st.rerun()
                            except Exception as _lpe:
                                st.error(f"Save failed: {_lpe}")

                    # ── Frame / Boxing edit (inline toggle) ─────────────
                    if st.session_state.get(f"_bo_bpedit_{eye_label}_{idx}"):
                        import json as _bpj
                        _bp_cur = line.get("boxing_params") or {}
                        if isinstance(_bp_cur, str):
                            try: _bp_cur = _bpj.loads(_bp_cur)
                            except Exception as _e:
                                logger.warning("Suppressed error: %s", _e)
                                _bp_cur = {}
                        st.markdown("<div style='background:#0c1a2e;border-radius:6px;padding:8px;margin:4px 0'>",
                                    unsafe_allow_html=True)
                        st.markdown("**📐 Frame / Boxing Measurements**")
                        _bd1, _bd2, _bd3, _bd4, _bd5 = st.columns(5)
                        _a_box = _bd1.number_input("A", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("a_box") or _bp_cur.get("A") or 0.0),
                            step=0.1, format="%.1f", key=f"bp_a_{eye_label}_{idx}")
                        _b_box = _bd2.number_input("B", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("b_box") or _bp_cur.get("B") or 0.0),
                            step=0.1, format="%.1f", key=f"bp_b_{eye_label}_{idx}")
                        _ed = _bd3.number_input("ED", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("ed") or _bp_cur.get("ED") or 0.0),
                            step=0.1, format="%.1f", key=f"bp_ed_{eye_label}_{idx}")
                        _ed_axis = _bd4.number_input("ED Axis", min_value=0, max_value=180,
                            value=int(float(_bp_cur.get("ed_axis") or _bp_cur.get("ED Axis") or 0)),
                            step=1, key=f"bp_ed_axis_{eye_label}_{idx}")
                        _dbl = _bd5.number_input("DBL", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("dbl") or _bp_cur.get("DBL") or 0.0),
                            step=0.1, format="%.1f", key=f"bp_dbl_{eye_label}_{idx}")

                        _pd1, _pd2, _pd3, _pd4, _pd5 = st.columns(5)
                        _r_pd = _pd1.number_input("R PD", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("r_pd") or _bp_cur.get("R PD") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_rpd_{eye_label}_{idx}")
                        _l_pd = _pd2.number_input("L PD", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("l_pd") or _bp_cur.get("L PD") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_lpd_{eye_label}_{idx}")
                        _ipd = _pd3.number_input("IPD", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("ipd") or _bp_cur.get("IPD") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_ipd_{eye_label}_{idx}")
                        _fh_r = _pd4.number_input("Fit HT R", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("fitting_ht_r") or _bp_cur.get("fit_ht_r") or _bp_cur.get("Fit HT R") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_fhr_{eye_label}_{idx}")
                        _fh_l = _pd5.number_input("Fit HT L", min_value=0.0, max_value=99.9,
                            value=float(_bp_cur.get("fitting_ht_l") or _bp_cur.get("fit_ht_l") or _bp_cur.get("Fit HT L") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_fhl_{eye_label}_{idx}")

                        _ang1, _ang2, _ang3 = st.columns(3)
                        _panto = _ang1.number_input("Panto", min_value=0.0, max_value=25.0,
                            value=float(_bp_cur.get("panto") or _bp_cur.get("Panto") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_panto_{eye_label}_{idx}")
                        _tilt = _ang2.number_input("Tilt", min_value=0.0, max_value=25.0,
                            value=float(_bp_cur.get("tilt") or _bp_cur.get("Tilt") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_tilt_{eye_label}_{idx}")
                        _bvd = _ang3.number_input("BVD", min_value=0.0, max_value=30.0,
                            value=float(_bp_cur.get("bvd") or _bp_cur.get("BVD") or 0.0),
                            step=0.5, format="%.1f", key=f"bp_bvd_{eye_label}_{idx}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        if st.button("💾 Save Boxing", key=f"bp_save_{eye_label}_{idx}",
                                     type="primary", use_container_width=True):
                            _bp_new = {
                                "a_box": round(_a_box, 1), "b_box": round(_b_box, 1),
                                "ed": round(_ed, 1), "ed_axis": int(_ed_axis),
                                "dbl": round(_dbl, 1), "r_pd": round(_r_pd, 1),
                                "l_pd": round(_l_pd, 1), "ipd": round(_ipd, 1),
                                "fitting_ht_r": round(_fh_r, 1), "fitting_ht_l": round(_fh_l, 1),
                                "panto": round(_panto, 1), "tilt": round(_tilt, 1),
                                "bvd": round(_bvd, 1),
                            }
                            line["boxing_params"] = _bp_new
                            try:
                                from modules.sql_adapter import run_write as _rw_bp
                                _rw_bp("UPDATE order_lines SET boxing_params=%(bp)s::jsonb WHERE id=%(lid)s::uuid",
                                       {"bp": _bpj.dumps(_bp_new),
                                        "lid": str(line.get("line_id") or line.get("id") or "")})
                                st.success("✅ Boxing saved")
                                st.session_state[f"_bo_bpedit_{eye_label}_{idx}"] = False
                                st.rerun()
                            except Exception as _bpe:
                                st.error(f"Save failed: {_bpe}")

                    # ── Admin price / discount override ─────────────────
                    try:
                        from modules.security.roles import ADMIN as _ADMIN_ROLE, MANAGER as _MANAGER_ROLE, has_role as _has_role_price
                        _can_edit_price = _has_role_price(_ADMIN_ROLE, _MANAGER_ROLE)
                    except Exception:
                        _can_edit_price = False
                    if _can_edit_price and not bool(line.get("is_service_line")):
                        with st.expander("💰 Price / Discount Override", expanded=False):
                            st.caption("Admin/Manager only. Sets manual price lock so discount refresh will not overwrite this line.")
                            _qty_price = int(line.get("billing_qty") or line.get("quantity") or 1)
                            _pr1, _pr2, _pr3 = st.columns(3)
                            _new_unit_price = _pr1.number_input(
                                "Unit price",
                                min_value=0.0,
                                value=float(line.get("unit_price") or 0),
                                step=10.0,
                                key=f"bo_price_up_{eye_label}_{idx}",
                            )
                            _new_disc_pct = _pr2.number_input(
                                "Discount %",
                                min_value=0.0,
                                max_value=100.0,
                                value=float(line.get("discount_percent") or 0),
                                step=0.5,
                                key=f"bo_price_dp_{eye_label}_{idx}",
                            )
                            _new_gst_pct = _pr3.number_input(
                                "GST %",
                                min_value=0.0,
                                max_value=28.0,
                                value=float(line.get("gst_percent") or 0),
                                step=0.5,
                                key=f"bo_price_gst_{eye_label}_{idx}",
                            )
                            _gross_manual = round(float(_new_unit_price or 0) * _qty_price, 2)
                            _disc_manual = round(_gross_manual * float(_new_disc_pct or 0) / 100, 2)
                            _net_manual = round(max(0.0, _gross_manual - _disc_manual), 2)
                            try:
                                from modules.core.price_qty_governor import compute_line_gst as _bo_compute_gst
                                _gst_calc = _bo_compute_gst(
                                    (_net_manual / max(_qty_price, 1)),
                                    _qty_price,
                                    _new_gst_pct,
                                    _order_type_live,
                                )
                                _gst_manual = float(_gst_calc.get("gst_amount") or 0)
                                _grand_manual = float(_gst_calc.get("grand_total") or _net_manual)
                            except Exception:
                                _gst_manual = round(_net_manual * float(_new_gst_pct or 0) / 100, 2)
                                _grand_manual = _net_manual if _order_type_live == "RETAIL" else round(_net_manual + _gst_manual, 2)
                            st.caption(
                                f"Gross ₹{_gross_manual:,.2f} · Discount ₹{_disc_manual:,.2f} · "
                                f"Billing ₹{_net_manual:,.2f} · GST ₹{_gst_manual:,.2f} · Grand ₹{_grand_manual:,.2f}"
                            )
                            _override_reason = st.text_input(
                                "Reason / note",
                                value=str((line.get("lens_params") or {}).get("manual_price_reason") if isinstance(line.get("lens_params"), dict) else ""),
                                key=f"bo_price_reason_{eye_label}_{idx}",
                            )
                            _complimentary_ok = st.checkbox(
                                "Allow zero price intentionally",
                                value=False,
                                key=f"bo_price_zero_ok_{eye_label}_{idx}",
                            )
                            if st.button("💾 Save Price Override", key=f"bo_price_save_{eye_label}_{idx}",
                                         type="primary", use_container_width=True):
                                if _new_unit_price <= 0 and not _complimentary_ok:
                                    st.error("Unit price is zero. Tick intentional zero price, or enter a price.")
                                else:
                                    try:
                                        import json as _price_json
                                        from modules.sql_adapter import run_write as _rw_price
                                        _lp_price = line.get("lens_params") or {}
                                        if isinstance(_lp_price, str):
                                            try: _lp_price = _price_json.loads(_lp_price) or {}
                                            except Exception: _lp_price = {}
                                        _lp_price["manual_price_override"] = True
                                        _lp_price["price_locked"] = True
                                        _lp_price["manual_price_reason"] = str(_override_reason or "").strip()
                                        _lp_price["discount_status"] = "APPLIED" if _disc_manual > 0 else "MANUAL"
                                        _rw_price("""
                                            UPDATE order_lines
                                            SET unit_price=%(up)s,
                                                total_price=%(tp)s,
                                                billing_total=%(tp)s,
                                                gst_percent=%(gp)s,
                                                gst_amount=%(ga)s,
                                                discount_percent=%(dp)s,
                                                discount_amount=%(da)s,
                                                discount_rule=%(dr)s,
                                                lens_params=%(lp)s::jsonb
                                            WHERE id=%(lid)s::uuid
                                        """, {
                                            "up": float(_new_unit_price or 0),
                                            "tp": _net_manual,
                                            "gp": float(_new_gst_pct or 0),
                                            "ga": _gst_manual,
                                            "dp": float(_new_disc_pct or 0),
                                            "da": _disc_manual,
                                            "dr": "Manual Price Override" if (_disc_manual or _override_reason) else "",
                                            "lp": _price_json.dumps(_lp_price),
                                            "lid": str(line.get("line_id") or line.get("id") or ""),
                                        })
                                        line["unit_price"] = float(_new_unit_price or 0)
                                        line["total_price"] = _net_manual
                                        line["billing_total"] = _net_manual
                                        line["gst_percent"] = float(_new_gst_pct or 0)
                                        line["gst_amount"] = _gst_manual
                                        line["discount_percent"] = float(_new_disc_pct or 0)
                                        line["discount_amount"] = _disc_manual
                                        line["discount_rule"] = "Manual Price Override" if (_disc_manual or _override_reason) else ""
                                        line["lens_params"] = _lp_price
                                        try:
                                            from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                                            refresh_order_pricing_rules(order, persist=True)
                                        except Exception as _price_sync_err:
                                            logger.warning("Price override pricing sync failed: %s", _price_sync_err)
                                        _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                        st.session_state.pop(f"_bo_disc_val_done_{order_id}", None)
                                        st.success("✅ Price override saved")
                                        st.rerun()
                                    except Exception as _price_err:
                                        st.error(f"Price save failed: {_price_err}")

                    # ── Services expander (all orders) ────────────────────
                    with st.expander("🔧 Services", expanded=False):
                        st.info(
                            "Service adding is now handled from Billing Summary → "
                            "Add missed service / collectable before billing. "
                            "This keeps Colouring, Fitting, Courier and other charges "
                            "on one punching-style flow and prevents duplicate service lines."
                        )
                        _SCT = {
                            "COLOURING": {"label":"Colouring/Tint","icon":"🎨","color":"#8b5cf6","default_gst":18},
                            "FITTING":   {"label":"Fitting",       "icon":"🔧","color":"#f59e0b","default_gst":18},
                            "COURIER":   {"label":"Courier",       "icon":"🚚","color":"#3b82f6","default_gst":18},
                            "OTHER":     {"label":"Other",         "icon":"➕","color":"#64748b","default_gst":18},
                            "MISC":      {"label":"Other",         "icon":"➕","color":"#64748b","default_gst":18},
                            "CONSULTATION": {"label":"Consultation","icon":"🩺","color":"#22c55e","default_gst":0},
                            "EYE_TESTING":  {"label":"Eye Testing", "icon":"👁️","color":"#10b981","default_gst":0},
                        }
                        try:
                            from modules.backoffice.service_master import fetch_service_types, service_price, suggested_provider_for_service
                            _svc_master_rows = fetch_service_types(active_only=True)
                        except Exception:
                            _svc_master_rows = []
                            service_price = lambda svc, ot, *args, **kwargs: float((svc or {}).get("default_price") or 0)
                            suggested_provider_for_service = lambda code: None
                        _svc_master_rows = _bo_complete_service_rows(_svc_master_rows)

                        _svc_by_group = {}
                        for _svcm in _svc_master_rows:
                            _svc_by_group.setdefault(str(_svcm.get("service_group") or "OTHER").upper(), []).append(_svcm)

                        # Load existing service lines for this order
                        try:
                            from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
                            _order_id_svc = _resolve_order_uuid(order.get("id") or order.get("order_id") or order.get("order_no")) or ""
                        except Exception:
                            _order_id_svc = ""
                        _existing_svc = {}
                        if _order_id_svc:
                            try:
                                from modules.sql_adapter import run_query as _rq_svc
                                _svc_rows = _rq_svc(
                                    """SELECT ol.id::text, ol.lens_params->>'charge_type' AS charge_type,
                                              COALESCE(p.product_name,
                                                       ol.lens_params->>'service_display_name',
                                                       ol.lens_params->>'display_product_name',
                                                       ol.lens_params->>'service_description',
                                                       'Service') AS product_name,
                                              ol.unit_price, ol.gst_percent
                                       FROM order_lines ol
                                       LEFT JOIN products p ON p.id = ol.product_id
                                       WHERE ol.order_id=%(oid)s::uuid
                                         AND COALESCE(ol.is_service_line,FALSE)=TRUE
                                         AND UPPER(COALESCE(ol.eye_side,'')) IN ('S','SERVICE')
                                         AND COALESCE(ol.is_deleted,FALSE)=FALSE""",
                                    {"oid": _order_id_svc}
                                ) or []
                                for _sr in _svc_rows:
                                    _ct = str(_sr.get("charge_type") or "").upper()
                                    if _ct:
                                        _existing_svc[_ct] = _sr
                            except Exception:
                                pass

                        _svc_order = []
                        for _svc_type in _svc_order:
                            if _svc_type not in _svc_by_group and _svc_type not in _existing_svc:
                                continue
                            _scfg = _SCT.get(_svc_type, {})
                            _s_icon  = _scfg.get("icon","🔧")
                            _s_label = _scfg.get("label", _svc_type.title())
                            _s_color = _scfg.get("color","#64748b")
                            _s_gst   = float(_scfg.get("default_gst", 18))
                            _s_existing = _existing_svc.get(_svc_type)

                            st.markdown(
                                f"<div style='font-size:0.72rem;font-weight:700;color:{_s_color};"
                                f"margin-top:6px'>{_s_icon} {_s_label}</div>",
                                unsafe_allow_html=True
                            )
                            _group_rows = _svc_by_group.get(_svc_type, [])
                            _s_opts = ["None"] + [r.get("service_name") for r in _group_rows]
                            _svc_by_name = {r.get("service_name"): r for r in _group_rows}
                            _s_sel = st.selectbox(
                                f"{_s_label}",
                                _s_opts,
                                index=0,
                                key=f"svc_{_svc_type}_{eye_label}_{idx}",
                                label_visibility="collapsed"
                            )

                            if _s_sel and _s_sel != "None":
                                _svc_def = _svc_by_name.get(_s_sel) or {}
                                _svc_code = _svc_def.get("service_code") or _svc_type
                                _default_amt = service_price(
                                    _svc_def,
                                    _order_type_live,
                                    party_id=str(order.get("party_id") or ""),
                                ) if _svc_def else 0.0
                                if float(_default_amt or 0) <= 0:
                                    _default_amt = float(_svc_def.get("default_price") or 0)
                                _s_gst = float(_svc_def.get("gst_percent") or _s_gst)
                                _provider_hint = suggested_provider_for_service(_svc_code) if _svc_code else None
                                try:
                                    from modules.security.roles import ADMIN as _ADMIN_ROLE, MANAGER as _MANAGER_ROLE, has_role as _has_role_bo
                                    _can_override_service_price = _has_role_bo(_ADMIN_ROLE, _MANAGER_ROLE)
                                except Exception:
                                    _can_override_service_price = False
                                _sc1, _sc2, _scq, _sc3 = st.columns([2, 1.2, 1.0, 1])
                                if _provider_hint:
                                    st.caption(
                                        f"Suggested provider: {_provider_hint.get('provider_name')} "
                                        f"· purchase ₹{float(_provider_hint.get('purchase_rate') or 0):,.0f}"
                                    )
                                _s_amt = _sc2.number_input(
                                    "₹ Rate / pair", min_value=0.0, step=10.0,
                                    value=float(_default_amt or 0.0),
                                    key=f"svc_amt_{_svc_type}_{eye_label}_{idx}_{_s_sel}",
                                    disabled=not _can_override_service_price,
                                    help="Admin/Manager can override. Others use Service Master price."
                                )
                                _svc_qty_factor = _scq.selectbox(
                                    "Qty ⚠️ required",
                                    [0.5, 1.0, 1.5, 2.0, 3.0],
                                    index=1,
                                    format_func=lambda v: (
                                        f"{v:g} pair — 1 eye" if v == 0.5 else
                                        f"{v:g} pair — both eyes" if v == 1.0 else
                                        f"{v:g} pair"
                                    ),
                                    key=f"svc_qty_factor_{_svc_type}_{eye_label}_{idx}",
                                    help="0.5 pair = treating 1 eye only. 1 pair = treating both eyes. Mandatory.",
                                )
                                _s_gst_inp = _sc3.number_input(
                                    "GST%", min_value=0.0, max_value=28.0,
                                    value=_s_gst, step=0.5,
                                    key=f"svc_gst_{_svc_type}_{eye_label}_{idx}"
                                )
                                _svc_instruction = ""
                                _svc_photo_b64 = ""
                                _svc_photo_name = ""
                                if _svc_type in ("COLOURING", "FITTING", "OTHER", "MISC", "CONSULTATION", "EYE_TESTING"):
                                    _svc_instruction = st.text_area(
                                        "Special instruction for provider",
                                        placeholder="Tint shade, colour sample instruction, fitting note, urgency...",
                                        key=f"svc_instr_{_svc_type}_{eye_label}_{idx}",
                                        height=70,
                                    )
                                    if _svc_type == "COLOURING":
                                        _svc_photo = st.file_uploader(
                                            "Colour sample photograph",
                                            type=["jpg", "jpeg", "png", "webp"],
                                            key=f"svc_colour_sample_{_svc_type}_{eye_label}_{idx}",
                                        )
                                        if _svc_photo:
                                            import base64 as _svc_b64
                                            _svc_photo_b64 = _svc_b64.b64encode(_svc_photo.read()).decode("ascii")
                                            _svc_photo_name = _svc_photo.name
                                with _sc1:
                                    if st.button(f"➕ Add {_s_label}",
                                                 key=f"svc_add_{_svc_type}_{eye_label}_{idx}",
                                                 use_container_width=True):
                                        if _s_amt >= 0 and _order_id_svc:
                                            try:
                                                import uuid as _suuid, json as _sj
                                                from modules.sql_adapter import run_write as _rw_svc
                                                _line_base = round(float(_s_amt or 0) * float(_svc_qty_factor or 1), 2)
                                                # Courier/Other/Consultation → direct billing.
                                                # Colouring/Fitting → production.
                                                _direct = (_svc_type == "COURIER")
                                                if _svc_type not in ("COLOURING", "FITTING"):
                                                    _direct = True
                                                _svc_route = _service_route_for_group(_svc_type, _direct)
                                                _lp_svc = _sj.dumps({
                                                    "charge_type": _svc_type,
                                                    "service_type": _svc_type,
                                                    "service_code": _svc_code,
                                                    "service_description": _s_sel,
                                                    "service_display_name": f"{_s_label}: {_s_sel}",
                                                    "display_product_name": f"{_s_label}: {_s_sel}",
                                                    "service_production_type": "" if _direct else _svc_type,
                                                    "service_origin": "backoffice",
                                                    "manufacturing_route": _svc_route,
                                                    "suggested_provider_id": (_provider_hint or {}).get("id"),
                                                    "suggested_provider_name": (_provider_hint or {}).get("provider_name"),
                                                    "suggested_provider_phone": (_provider_hint or {}).get("contact"),
                                                    "service_instruction": _svc_instruction,
                                                    "colour_sample_photo": _svc_photo_b64,
                                                    "colour_sample_filename": _svc_photo_name,
                                                    "price_overridden": bool(_can_override_service_price and abs(float(_s_amt or 0) - float(_default_amt or 0)) > 0.001),
                                                    "service_qty_factor": float(_svc_qty_factor or 1),
                                                    "service_rate_per_pair": float(_s_amt or 0),
                                                })
                                                _total, _gst_a = _bo_service_line_amounts(
                                                    _line_base, _s_gst_inp, _order_type_live
                                                )
                                                _svc_pid = ""
                                                try:
                                                    _prod_name_svc = f"{_s_label}: {_s_sel}"
                                                    _svc_pid = _ensure_bo_service_product(_svc_type, _prod_name_svc, _s_gst_inp)
                                                except Exception:
                                                    _svc_pid = None
                                                _dup_key_svc = f"bo_dup_svc_confirm_{_order_id_svc}_{_svc_code}"
                                                if _s_existing and not st.session_state.get(_dup_key_svc):
                                                    st.warning(f"{_s_label} already exists on this order. Click again to add another copy.")
                                                    st.session_state[_dup_key_svc] = True
                                                    st.stop()
                                                _rw_svc("""
                                                    INSERT INTO order_lines
                                                      (id, order_id, product_id, eye_side,
                                                       unit_price, total_price, billing_total,
                                                       gst_percent, gst_amount,
                                                       quantity, billing_qty,
                                                       allocated_qty, is_service_line,
                                                       batch_status, lens_params)
                                                    VALUES
                                                      (%(id)s::uuid, %(oid)s::uuid, %(pid)s::uuid, 'S',
                                                       %(up)s, %(tp)s, %(tp)s,
                                                       %(gp)s, %(ga)s,
                                                       1, 1,
                                                       %(aq)s, TRUE,
                                                       %(bs)s, %(lp)s::jsonb)
                                                """, {
                                                    "id":  str(_suuid.uuid4()),
                                                    "oid": _order_id_svc,
                                                    "pid": _svc_pid,
                                                    "up":  _line_base,
                                                    "tp":  _total,
                                                    "gp":  _s_gst_inp,
                                                    "ga":  _gst_a,
                                                    "aq":  1 if _direct else 0,
                                                    "bs":  "READY" if _direct else "PENDING",
                                                    "lp":  _lp_svc,
                                                })
                                                try:
                                                    from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                                                    ensure_order_production_refs(order_id=_order_id_svc)
                                                except Exception as _svc_ref_err:
                                                    logger.warning("Quick service production_ref fill failed: %s", _svc_ref_err)
                                                st.session_state.pop(_dup_key_svc, None)
                                                st.success(f"✅ {_s_label} added — ₹{_total:.0f}")
                                                st.rerun()
                                            except Exception as _se:
                                                st.error(f"Failed: {_se}")
                                        else:
                                            st.warning("Select a service first")

                            # Show existing service line if any
                            if _s_existing:
                                st.markdown(
                                    f"<div style='font-size:0.67rem;color:#10b981;"
                                    f"background:#022c22;padding:2px 6px;border-radius:4px;"
                                    f"margin-top:2px'>✅ Active: ₹{float(_s_existing.get('unit_price',0)):,.0f}"
                                    f" — {_s_existing.get('product_name','')}</div>",
                                    unsafe_allow_html=True
                                )

                    # ── Expander: Qty + Allocation (hidden by default) ──
                    with st.expander("📦 Qty & Allocation", expanded=False):
                        # Quantity
                        current_qty = int(line.get("billing_qty") or 1)
                        new_qty = st.number_input(
                            "Quantity", min_value=1, value=current_qty, step=1,
                            key=f"qty_{eye_label}_{idx}"
                        )
                        if new_qty != current_qty:
                            line["billing_qty"]      = int(new_qty)
                            line["batch_allocation"] = []
                            line["allocated_qty"]    = 0
                            line["batch_status"]     = "PENDING"
                            refresh_line_state(line)
                            recalculate_order_totals(order)
                            st.success(f"✅ Qty → {new_qty}")
                            st.rerun()
                        # Allocation
                        if st.button("🗂️ Manage Allocation", key=f"alloc_exp_{eye_label}_{idx}",
                                     use_container_width=True):
                            st.session_state.bo_show_allocation_window = True
                            st.session_state.bo_allocation_line_idx = idx
                            st.rerun()

                    with st.expander("🗑 Delete Line", expanded=False):
                        _line_id_del = str(line.get("line_id") or line.get("id") or "")
                        _can_delete_line = True
                        try:
                            from modules.sql_adapter import run_query as _rq_del_guard
                            _dg = _rq_del_guard("""
                                SELECT
                                    COALESCE(ol.billed_qty,0) AS billed_qty,
                                    EXISTS(
                                        SELECT 1 FROM challan_lines cl
                                        JOIN challans c ON c.id=cl.challan_id
                                        WHERE cl.order_line_id=ol.id
                                          AND c.status NOT IN ('CANCELLED','VOID')
                                    ) AS has_challan
                                FROM order_lines ol
                                WHERE ol.id=%(lid)s::uuid
                                LIMIT 1
                            """, {"lid": _line_id_del}) if _line_id_del else []
                            if _dg and (int(_dg[0].get("billed_qty") or 0) > 0 or bool(_dg[0].get("has_challan"))):
                                _can_delete_line = False
                        except Exception:
                            _can_delete_line = True
                        if not _can_delete_line:
                            st.warning("Line is already challaned/billed. Cancel challan first.")
                        else:
                            _del_key = f"bo_line_delete_confirm_{_line_id_del}"
                            if not st.session_state.get(_del_key):
                                if st.button("🗑 Delete this line", key=f"bo_line_delete_{_line_id_del}", use_container_width=True):
                                    st.session_state[_del_key] = True
                                    st.rerun()
                            else:
                                st.warning(f"Delete {_bo_line_display_name(line)}?")
                                _dl1, _dl2 = st.columns(2)
                                if _dl1.button("Yes, delete", key=f"bo_line_delete_yes_{_line_id_del}", type="primary"):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_line_del
                                        _bo_release_stock_allocation_for_line(line, "line_delete")
                                        _rw_line_del("""
                                            UPDATE order_lines
                                            SET is_deleted  = TRUE,
                                                status      = 'CANCELLED',
                                                deleted_at  = NOW(),
                                                deleted_by  = %(who)s
                                            WHERE id = %(lid)s::uuid
                                        """, {"lid": _line_id_del,
                                                "who": str(st.session_state.get("user_name") or "backoffice")})
                                        _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                        st.session_state[_del_key] = False
                                        st.success("✅ Line deleted")
                                        st.rerun()
                                    except Exception as _del_err:
                                        st.error(f"Delete failed: {_del_err}")
                                if _dl2.button("Cancel", key=f"bo_line_delete_no_{_line_id_del}"):
                                    st.session_state[_del_key] = False
                                    st.rerun()

                # ── Render R and L side by side ───────────────────────────
                if has_right:
                    with col_r:
                        _render_eye_block_ui(group['R'], group['R_idx'], 'R')

                if has_left:
                    with col_l:
                        _render_eye_block_ui(group['L'], group['L_idx'], 'L')

            # ── Services / other non-eye lines ────────────────────────────────
            _other_display_lines = [
                (idx, line) for idx, line in enumerate(all_lines)
                if str(line.get("eye_side", "")).upper() not in ["R", "RIGHT", "L", "LEFT"]
            ]
            if _other_display_lines:
                _svc_display_lines = _other_display_lines

                if _svc_display_lines:
                    st.markdown("#### 🔧 Services / Other Items")
                for _oi, _oln in _svc_display_lines:
                    _lp_o = _oln.get("lens_params") or {}
                    if isinstance(_lp_o, str):
                        try:
                            import json as _oln_json
                            _lp_o = _oln_json.loads(_lp_o)
                        except Exception:
                            _lp_o = {}
                    _oname = str(
                        _oln.get("product_name")
                        or _lp_o.get("service_display_name")
                        or _lp_o.get("display_product_name")
                        or _lp_o.get("service_description")
                        or "Service"
                    )
                    _oln_eye_side = str(_oln.get("eye_side") or "").upper()
                    _is_frame_line = (
                        _oln_eye_side in ("B", "BOTH", "FRAME")
                        and not bool(_oln.get("is_service_line"))
                    )
                    if _is_frame_line:
                        _svc_type = "FRAME"
                        _saved_frame_route = str(
                            _oln.get("manufacturing_route") or
                            _lp_o.get("manufacturing_route") or "STOCK"
                        ).upper()
                        _route = _saved_frame_route
                        _frame_route_wrong = False
                        _frame_available_qty = 0
                        _frame_ba = _lp_o.get("batch_allocation") or _oln.get("batch_allocation") or []
                        _frame_ba0 = _frame_ba[0] if isinstance(_frame_ba, list) and _frame_ba else {}
                        _frame_sku = str(
                            _lp_o.get("batch_no")
                            or _oln.get("batch_no")
                            or (_frame_ba0.get("batch_no") if isinstance(_frame_ba0, dict) else "")
                            or ""
                        ).strip()
                        _frame_stock_id = str(
                            _lp_o.get("stock_id")
                            or (_frame_ba0.get("stock_id") if isinstance(_frame_ba0, dict) else "")
                            or (_frame_ba0.get("batch_id") if isinstance(_frame_ba0, dict) else "")
                            or ""
                        ).strip()
                        _frame_meta = {}
                        try:
                            from modules.sql_adapter import run_query as _rq_frame_bo
                            _frame_stock_rows = _rq_frame_bo("""
                                SELECT
                                    COALESCE(SUM(
                                        GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0))
                                    ),0) AS available_qty,
                                    MAX(id::text) AS stock_id,
                                    MAX(batch_no) AS batch_no,
                                    MAX(COALESCE(colour_mix,'')) AS colour_mix,
                                    MAX(COALESCE(frame_group,'')) AS frame_group
                                FROM inventory_stock
                                WHERE (
                                       (%(sid)s != '' AND id::text = %(sid)s)
                                    OR (%(sku)s != '' AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%(sku)s)))
                                    OR (
                                          %(sid)s = '' AND %(sku)s = ''
                                          AND %(pid)s != ''
                                          AND product_id = %(pid)s::uuid
                                       )
                                )
                                  AND COALESCE(is_active, TRUE) = TRUE
                            """, {
                                "pid": str(_oln.get("product_id") or ""),
                                "sku": _frame_sku,
                                "sid": _frame_stock_id,
                            }) if _oln.get("product_id") else []
                            if _frame_stock_rows:
                                _frame_meta = _frame_stock_rows[0] or {}
                                _frame_available_qty = int(float((_frame_meta.get("available_qty") if _frame_meta else 0) or 0))
                                _frame_sku = _frame_sku or str(_frame_meta.get("batch_no") or "").strip()
                                _frame_stock_id = _frame_stock_id or str(_frame_meta.get("stock_id") or "").strip()
                        except Exception:
                            _frame_available_qty = 0
                        _frame_colour = str(
                            _lp_o.get("colour_mix") or _oln.get("colour_mix") or _frame_meta.get("colour_mix") or ""
                        ).strip()
                        _frame_group = str(
                            _lp_o.get("frame_group") or _oln.get("frame_group") or _frame_meta.get("frame_group") or ""
                        ).strip()
                        _frame_name_parts = [_oname]
                        if _frame_sku and _frame_sku not in _frame_name_parts:
                            _frame_name_parts.append(_frame_sku)
                        if _frame_colour:
                            _frame_name_parts.append(_frame_colour)
                        if _frame_group:
                            _frame_name_parts.append(_frame_group)
                        _oname = " | ".join([str(_p).strip() for _p in _frame_name_parts if str(_p).strip()])
                        _frame_already_alloc = int(_oln.get("allocated_qty") or 0) > 0
                        if _frame_already_alloc or _frame_available_qty > 0:
                            _frame_route_wrong = _saved_frame_route != "STOCK"
                            _route = "STOCK"
                            if _frame_route_wrong:
                                try:
                                    from modules.sql_adapter import run_write as _rw_frame_route_fix
                                    _rw_frame_route_fix("""
                                        UPDATE order_lines
                                        SET lens_params = CASE
                                            WHEN %(sid)s != '' THEN jsonb_set(
                                                jsonb_set(
                                                    COALESCE(lens_params, '{}'::jsonb),
                                                    '{manufacturing_route}', to_jsonb('STOCK'::text), TRUE
                                                ),
                                                '{stock_id}', to_jsonb(%(sid)s::text), TRUE
                                            )
                                            ELSE jsonb_set(
                                                COALESCE(lens_params, '{}'::jsonb),
                                                '{manufacturing_route}', to_jsonb('STOCK'::text), TRUE
                                            )
                                        END
                                        WHERE id = %(lid)s::uuid
                                    """, {
                                        "sid": _frame_stock_id,
                                        "lid": str(_oln.get("line_id") or _oln.get("id") or ""),
                                    })
                                    _frame_route_wrong = False
                                except Exception:
                                    pass
                        elif _saved_frame_route not in ("STOCK", "VENDOR"):
                            _route = "VENDOR"
                    else:
                        _frame_route_wrong = False
                        _frame_available_qty = 0
                        _frame_sku = ""
                        _svc_type = str(_lp_o.get("service_production_type") or _lp_o.get("charge_type") or "").upper()
                        _route = str(_oln.get("manufacturing_route") or _lp_o.get("manufacturing_route") or "SERVICE").upper()
                    _qty_factor = _lp_o.get("service_qty_factor")
                    if _is_frame_line:
                        _qty_text = f"{int(_oln.get('billing_qty') or _oln.get('quantity') or 1)} PCS"
                    else:
                        _qty_text = f"{float(_qty_factor):g} pair" if _qty_factor not in (None, "", 0) else f"{int(_oln.get('billing_qty') or 1)}"
                    _total = float(_oln.get("billing_total") or 0)
                    _gst = float(_oln.get("gst_percent") or 0)
                    _badge_color = "#a78bfa" if _svc_type == "COLOURING" else ("#f59e0b" if _svc_type == "FITTING" else "#22c55e")
                    st.markdown(
                        f"<div style='background:#0f172a;border:1px solid #1e293b;"
                        f"border-left:3px solid {_badge_color};border-radius:6px;"
                        f"padding:9px 12px;margin:6px 0'>"
                        f"<div style='display:flex;justify-content:space-between;gap:10px;align-items:center'>"
                        f"<div><b style='color:#f8fafc'>{_oname}</b>"
                        f"<div style='font-size:0.72rem;color:#94a3b8'>"
                        f"{_svc_type or 'SERVICE'} · Route {_route} · Qty {_qty_text}</div></div>"
                        f"<div style='text-align:right;color:#f8fafc;font-weight:800'>₹{_total:,.2f}"
                        f"<div style='font-size:0.68rem;color:#94a3b8;font-weight:500'>{_gst:g}% GST</div></div>"
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )
                    _svc_note = str(_lp_o.get("service_instruction") or "").strip()
                    if _svc_note:
                        st.caption(f"Instruction: {_svc_note}")
                    if _is_frame_line:
                        _frame_alloc = int(_oln.get("allocated_qty") or 0)
                        _frame_need = max(0, int(_oln.get("billing_qty") or _oln.get("quantity") or 1) - _frame_alloc)
                        if _frame_route_wrong:
                            st.warning("Frame route was saved as VENDOR earlier, but matching stock exists. Route shown as STOCK.")
                        if _frame_need > 0 and _frame_available_qty <= 0 and _route == "STOCK":
                            st.error("Frame is marked Stock but no inventory is available for this SKU. Receive stock or change to vendor arrangement.")
                        elif _frame_need > 0 and _frame_available_qty <= 0:
                            st.warning("Frame is not available in inventory. Proceed only if this frame will be arranged from vendor.")
                        elif _frame_need > 0 and _frame_available_qty > 0:
                            _assign_key = f"bo_assign_frame_stock_{str(_oln.get('line_id') or _oln.get('id') or '')}"
                            if st.button("✅ Assign frame from inventory", key=_assign_key, use_container_width=True):
                                try:
                                    from modules.sql_adapter import run_write as _rw_frame_bo
                                    _rw_frame_bo("""
                                        WITH target AS (
                                            SELECT id, batch_no
                                            FROM inventory_stock
                                            WHERE (
                                                   (%(sid)s != '' AND id::text = %(sid)s)
                                                OR (%(sku)s != '' AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%(sku)s)))
                                                OR (
                                                      %(sid)s = '' AND %(sku)s = ''
                                                      AND %(pid)s != ''
                                                      AND product_id = %(pid)s::uuid
                                                   )
                                            )
                                              AND COALESCE(is_active, TRUE) = TRUE
                                              AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) >= %(qty)s
                                            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                                            LIMIT 1
                                        ), upd_stock AS (
                                            UPDATE inventory_stock s
                                            SET allocated_qty = COALESCE(allocated_qty,0) + %(qty)s,
                                                updated_at = NOW()
                                            FROM target t
                                            WHERE s.id = t.id
                                            RETURNING s.id, s.batch_no
                                        )
                                        UPDATE order_lines ol
                                        SET allocated_qty = COALESCE(allocated_qty,0) + %(qty)s,
                                            ready_qty = GREATEST(COALESCE(ready_qty,0), COALESCE(allocated_qty,0) + %(qty)s),
                                            status = 'READY',
                                            lens_params = jsonb_set(
                                                jsonb_set(
                                                    jsonb_set(
                                                        COALESCE(ol.lens_params, '{}'::jsonb),
                                                        '{manufacturing_route}', to_jsonb('STOCK'::text), TRUE
                                                    ),
                                                    '{stock_id}', to_jsonb((SELECT id::text FROM upd_stock LIMIT 1)), TRUE
                                                ),
                                                '{batch_allocation}',
                                                jsonb_build_array(jsonb_build_object(
                                                    'stock_id', (SELECT id::text FROM upd_stock LIMIT 1),
                                                    'batch_id', (SELECT id::text FROM upd_stock LIMIT 1),
                                                    'batch_no', (SELECT batch_no FROM upd_stock LIMIT 1),
                                                    'allocated_qty', %(qty)s
                                                )),
                                                TRUE
                                            )
                                        WHERE ol.id = %(lid)s::uuid
                                          AND EXISTS (SELECT 1 FROM upd_stock)
                                    """, {
                                        "pid": str(_oln.get("product_id") or ""),
                                        "sku": _frame_sku,
                                        "sid": _frame_stock_id,
                                        "qty": _frame_need,
                                        "lid": str(_oln.get("line_id") or _oln.get("id") or ""),
                                    })
                                    st.success("Frame assigned from inventory.")
                                    st.rerun()
                                except Exception as _frame_assign_e:
                                    st.error(f"Frame assignment failed: {_frame_assign_e}")
                    if _tab1_edit_locked:
                        continue
                    _exp_lbl = "✏️ Edit / Delete Frame" if _is_frame_line else "✏️ Edit / Delete Service"
                    with st.expander(_exp_lbl, expanded=False):
                        if True:
                            _sid = str(_oln.get("line_id") or _oln.get("id") or "")
                            _old_total = _bo_float(_oln.get("billing_total") or _oln.get("total_price"))
                            _old_gst = _bo_float(_oln.get("gst_percent"))
                            if _is_frame_line:
                                _old_qty = max(1, int(_oln.get("billing_qty") or _oln.get("quantity") or 1))
                                _old_rate = round(_old_total / _old_qty, 2) if _old_qty else _old_total
                                _ec1, _ec2, _ec3 = st.columns([1, 1, 1])
                                _new_qty = _ec1.number_input(
                                    "Qty (PCS)",
                                    min_value=1,
                                    value=int(_old_qty),
                                    step=1,
                                    key=f"bo_frame_edit_qty_{_sid}",
                                )
                                _new_rate = _ec2.number_input(
                                    "Rate / pc",
                                    min_value=0.0,
                                    value=float(_old_rate or 0),
                                    step=10.0,
                                    key=f"bo_frame_edit_rate_{_sid}",
                                )
                                _new_gst = _ec3.number_input(
                                    "GST%",
                                    min_value=0.0,
                                    max_value=28.0,
                                    value=float(_old_gst or 0),
                                    step=0.5,
                                    key=f"bo_frame_edit_gst_{_sid}",
                                )
                                _new_total = round(float(_new_rate or 0) * int(_new_qty or 1), 2)
                                _new_gst_amt = round(_new_total - (_new_total / (1 + float(_new_gst or 0) / 100)), 2) if _new_gst else 0.0
                                st.caption(f"New amount: ₹{_new_total:,.2f} · GST ₹{_new_gst_amt:,.2f}")
                                _fb1, _fb2 = st.columns(2)
                                if _fb1.button("💾 Save Frame", key=f"bo_frame_edit_save_{_sid}", use_container_width=True):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_frame_edit
                                        _rw_frame_edit("""
                                            UPDATE order_lines
                                            SET quantity=%(qty)s,
                                                unit_price=%(up)s,
                                                total_price=%(tp)s,
                                                billing_total=%(tp)s,
                                                gst_percent=%(gp)s,
                                                gst_amount=%(ga)s
                                            WHERE id=%(lid)s::uuid
                                        """, {
                                            "qty": int(_new_qty or 1),
                                            "up": float(_new_rate or 0),
                                            "tp": float(_new_total or 0),
                                            "gp": float(_new_gst or 0),
                                            "ga": float(_new_gst_amt or 0),
                                            "lid": _sid,
                                        })
                                        _oln["quantity"] = int(_new_qty or 1)
                                        _oln["billing_qty"] = int(_new_qty or 1)
                                        _oln["unit_price"] = float(_new_rate or 0)
                                        _oln["total_price"] = float(_new_total or 0)
                                        _oln["billing_total"] = float(_new_total or 0)
                                        _oln["gst_percent"] = float(_new_gst or 0)
                                        _oln["gst_amount"] = float(_new_gst_amt or 0)
                                        try:
                                            from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                                            refresh_order_pricing_rules(order, persist=True)
                                        except Exception as _frame_sync_err:
                                            logger.warning("Frame edit pricing sync failed: %s", _frame_sync_err)
                                        _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                        st.success("Frame updated.")
                                        st.rerun()
                                    except Exception as _frame_edit_e:
                                        st.error(f"Frame update failed: {_frame_edit_e}")
                                if _fb2.button("🗑️ Delete Frame", key=f"bo_frame_del_{_sid}", use_container_width=True):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_frame_del
                                        _rw_frame_del(
                                            "UPDATE order_lines SET is_deleted=TRUE WHERE id=%(lid)s::uuid",
                                            {"lid": _sid},
                                        )
                                        _oln["is_deleted"] = True
                                        try:
                                            from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                                            refresh_order_pricing_rules(order, persist=True)
                                        except Exception as _frame_del_sync_err:
                                            logger.warning("Frame delete pricing sync failed: %s", _frame_del_sync_err)
                                        _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                        st.warning("Frame deleted from order.")
                                        st.rerun()
                                    except Exception as _frame_del_e:
                                        st.error(f"Frame delete failed: {_frame_del_e}")
                                continue

                            _old_factor = _bo_float(_lp_o.get("service_qty_factor"), 1.0) or 1.0
                            _rate_pair = _bo_float(_lp_o.get("service_rate_per_pair"))
                            if _rate_pair <= 0 and _old_factor > 0:
                                _rate_pair = round(_old_total / _old_factor, 2)
                            _ec1, _ec2, _ec3 = st.columns([1, 1, 1])
                            _new_factor = _ec1.selectbox(
                                "Qty",
                                [0.5, 1.0, 1.5, 2.0, 3.0],
                                index=[0.5, 1.0, 1.5, 2.0, 3.0].index(_old_factor) if _old_factor in [0.5, 1.0, 1.5, 2.0, 3.0] else 1,
                                format_func=lambda v: f"{v:g} pair" if v != 0.5 else "0.5 pair — one eye",
                                key=f"bo_svc_edit_qty_{_sid}",
                            )
                            _new_rate = _ec2.number_input(
                                "Rate / pair",
                                min_value=0.0,
                                value=float(_rate_pair or 0),
                                step=10.0,
                                key=f"bo_svc_edit_rate_{_sid}",
                            )
                            _new_gst = _ec3.number_input(
                                "GST%",
                                min_value=0.0,
                                max_value=28.0,
                                value=float(_old_gst or 0),
                                step=0.5,
                                key=f"bo_svc_edit_gst_{_sid}",
                            )
                            _new_base = round(float(_new_rate or 0) * float(_new_factor or 1), 2)
                            _new_total, _new_gst_amt = _bo_service_line_amounts(_new_base, _new_gst, order.get("order_type"))
                            st.caption(f"New amount: ₹{_new_total:,.2f} · GST ₹{_new_gst_amt:,.2f}")
                            _eb1, _eb2 = st.columns(2)
                            if _eb1.button("💾 Save Service", key=f"bo_svc_edit_save_{_sid}", use_container_width=True):
                                try:
                                    import json as _svc_edit_json
                                    from modules.sql_adapter import run_write as _rw_svc_edit
                                    _lp_new = dict(_lp_o)
                                    _lp_new["service_qty_factor"] = float(_new_factor)
                                    _lp_new["service_rate_per_pair"] = float(_new_rate or 0)
                                    _rw_svc_edit("""
                                        UPDATE order_lines
                                        SET unit_price=%(up)s,
                                            total_price=%(tp)s,
                                            billing_total=%(tp)s,
                                            gst_percent=%(gp)s,
                                            gst_amount=%(ga)s,
                                            lens_params=%(lp)s::jsonb
                                        WHERE id=%(lid)s::uuid
                                    """, {
                                        "up": _new_base,
                                        "tp": _new_total,
                                        "gp": _new_gst,
                                        "ga": _new_gst_amt,
                                        "lp": _svc_edit_json.dumps(_lp_new),
                                        "lid": _sid,
                                    })
                                    _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                    st.success("✅ Service updated")
                                    # F3: audit service edit so changes appear in History tab
                                    try:
                                        from modules.backoffice.audit_logger import audit, AuditAction
                                        audit(
                                            AuditAction.PRICE_OVERRIDE,
                                            entity="order_lines",
                                            entity_id=_sid,
                                            order_id=str(order.get("id") or order.get("order_id") or ""),
                                            payload={
                                                "action":     "service_edit",
                                                "old_value":  str(round(_old_total, 2)),
                                                "new_value":  str(round(_new_total, 2)),
                                                "order_no":   str(order.get("order_no") or ""),
                                            },
                                        )
                                    except Exception as _audit_svc_err:
                                        logger.debug("Service edit audit write failed (non-fatal): %s", _audit_svc_err)
                                    st.rerun()
                                except Exception as _svc_edit_err:
                                    st.error(f"Service update failed: {_svc_edit_err}")
                            _confirm_key = f"bo_svc_delete_confirm_{_sid}"
                            if not st.session_state.get(_confirm_key):
                                if _eb2.button("🗑 Delete Service", key=f"bo_svc_delete_{_sid}", use_container_width=True):
                                    st.session_state[_confirm_key] = True
                                    st.rerun()
                            else:
                                st.warning("Delete this service line?")
                                _dc1, _dc2 = st.columns(2)
                                if _dc1.button("Yes, delete", key=f"bo_svc_delete_yes_{_sid}", type="primary"):
                                    try:
                                        from modules.sql_adapter import run_write as _rw_svc_del
                                        _rw_svc_del("""
                                            UPDATE order_lines
                                            SET is_deleted  = TRUE,
                                                status      = 'CANCELLED',
                                                deleted_at  = NOW(),
                                                deleted_by  = %(who)s
                                            WHERE id = %(lid)s::uuid
                                        """, {"lid": _sid,
                                                "who": str(st.session_state.get("user_name") or "backoffice")})
                                        _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                                        st.session_state[_confirm_key] = False
                                        st.success("✅ Service deleted")
                                        # F3: audit service delete so it appears in History tab
                                        try:
                                            from modules.backoffice.audit_logger import audit, AuditAction
                                            _deleted_amt = float(_oln.get("billing_total") or _oln.get("total_price") or 0)
                                            audit(
                                                AuditAction.PRICE_OVERRIDE,
                                                entity="order_lines",
                                                entity_id=_sid,
                                                order_id=str(order.get("id") or order.get("order_id") or ""),
                                                payload={
                                                    "action":    "service_delete",
                                                    "old_value": str(round(_deleted_amt, 2)),
                                                    "new_value": "0",
                                                    "order_no":  str(order.get("order_no") or ""),
                                                },
                                            )
                                        except Exception as _audit_del_err:
                                            logger.debug("Service delete audit write failed (non-fatal): %s", _audit_del_err)
                                        st.rerun()
                                    except Exception as _svc_del_err:
                                        st.error(f"Delete failed: {_svc_del_err}")
                                if _dc2.button("Cancel", key=f"bo_svc_delete_no_{_sid}"):
                                    st.session_state[_confirm_key] = False
                                    st.rerun()

            # ── Restore accidentally deleted lines before final lock ─────────────
            if not _tab1_edit_locked:
                try:
                    from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
                    _restore_order_id = _resolve_order_uuid(order.get("id") or order.get("order_id") or order.get("order_no")) or ""
                except Exception:
                    _restore_order_id = ""
                _deleted_lines = []
                if _restore_order_id:
                    try:
                        from modules.sql_adapter import run_query as _rq_restore
                        _deleted_lines = _rq_restore(
                            """
                            SELECT
                                ol.id::text AS line_id,
                                COALESCE(p.product_name, ol.lens_params->>'display_product_name', 'Line') AS product_name,
                                COALESCE(p.brand, '') AS brand,
                                COALESCE(ol.eye_side, '') AS eye_side,
                                COALESCE(ol.billing_qty, ol.quantity, 1) AS qty,
                                COALESCE(ol.unit_price, 0) AS unit_price,
                                COALESCE(ol.billing_total, ol.total_price, 0) AS billing_total,
                                COALESCE(ol.is_service_line, FALSE) AS is_service_line
                            FROM order_lines ol
                            LEFT JOIN products p ON p.id = ol.product_id
                            WHERE ol.order_id = %(oid)s::uuid
                              AND COALESCE(ol.is_deleted, FALSE) = TRUE
                              AND ol.deleted_at IS NOT NULL
                            ORDER BY ol.deleted_at DESC
                            """,
                            {"oid": _restore_order_id},
                        ) or []
                    except Exception as _restore_read_err:
                        logger.warning("Backoffice deleted-line restore read failed: %s", _restore_read_err)

                if _deleted_lines:
                    with st.expander("↩ Restore Deleted Lines", expanded=False):
                        st.caption("Deleted lines stay recoverable here until the order is finally saved/locked.")
                        for _dl in _deleted_lines:
                            _dlid = str(_dl.get("line_id") or "")
                            _eye = str(_dl.get("eye_side") or "").upper()
                            _eye_label = (
                                "R" if _eye in ("R", "RIGHT") else
                                "L" if _eye in ("L", "LEFT") else
                                "SVC" if bool(_dl.get("is_service_line")) else "OTHER"
                            )
                            _rc1, _rc2 = st.columns([4, 1])
                            _rc1.markdown(
                                f"**{_eye_label}** · {_dl.get('product_name') or 'Line'} "
                                f"· Qty {_dl.get('qty') or 1} · ₹{float(_dl.get('billing_total') or 0):,.2f}"
                            )
                            if _rc2.button("Restore", key=f"bo_restore_line_{_dlid}", use_container_width=True):
                                try:
                                    from modules.sql_adapter import run_write as _rw_restore
                                    _rw_restore(
                                        """
                                        UPDATE order_lines
                                        SET is_deleted = FALSE,
                                            status = CASE
                                                WHEN COALESCE(is_service_line, FALSE) THEN 'READY'
                                                ELSE 'PENDING'
                                            END
                                        WHERE id = %(lid)s::uuid
                                        """,
                                        {"lid": _dlid},
                                    )
                                    _bo_refresh_order_total_value(_restore_order_id)
                                    st.success("✅ Line restored")
                                    st.rerun()
                                except Exception as _restore_err:
                                    st.error(f"Restore failed: {_restore_err}")
        
            # ── Add Product / Service to Order ───────────────────────────────────
            st.markdown("---")
            _add_mode_key = f"bo_add_mode_{get_display_order_id(order)}"
            _add_mode = None if _tab1_edit_locked else st.session_state.get(_add_mode_key, None)
            if not _tab1_edit_locked:
                _ac1, _ac2, _ac3 = st.columns([1, 1, 3])
                with _ac1:
                    if st.button("➕ Add Product", key=f"bo_add_prod_{get_display_order_id(order)}",
                                 use_container_width=True):
                        st.session_state[_add_mode_key] = "PRODUCT" if _add_mode != "PRODUCT" else None
                        st.rerun()
                with _ac2:
                    if st.button("🔧 Add Service", key=f"bo_add_svc_{get_display_order_id(order)}",
                                 use_container_width=True):
                        st.session_state[_add_mode_key] = "SERVICE" if _add_mode != "SERVICE" else None
                        st.rerun()

            if _add_mode == "PRODUCT":
                import json as _apj
                st.markdown("<div style='background:#0f172a;border:1px solid #334155;"
                            "border-top:3px solid #0891b2;border-radius:8px;padding:12px;margin:6px 0'>",
                            unsafe_allow_html=True)
                st.markdown("**➕ Add Product to Order**")

                _ap1, _ap2, _ap3 = st.columns([3, 1, 1])
                _ap_search = _ap1.text_input("Search product name",
                                             key=f"bo_ap_search_{get_display_order_id(order)}",
                                             placeholder="Type product name...")
                _ap_eye = _ap2.selectbox("Eye", ["R","L","B","Both"],
                                         key=f"bo_ap_eye_{get_display_order_id(order)}")
                _ap_qty = _ap3.number_input("Qty", min_value=1, value=1, step=1,
                                            key=f"bo_ap_qty_{get_display_order_id(order)}")

                _ap_results = []
                if _ap_search and len(_ap_search) >= 2:
                    try:
                        from modules.sql_adapter import run_query as _rq_ap
                        _ap_results = _rq_ap(
                            """SELECT id::text, product_name, brand, main_group, unit,
                                      COALESCE(gst_percent, 0) AS gst_percent
                               FROM products
                               WHERE LOWER(product_name) LIKE LOWER(%(s)s)
                                 AND COALESCE(is_active, TRUE) = TRUE
                               ORDER BY product_name LIMIT 15""",
                            {"s": f"%{_ap_search}%"}
                        ) or []
                    except Exception as _ape:
                        st.error(f"Search error: {_ape}")

                if _ap_results:
                    _ap_names = [f"{r['product_name']} — {r.get('brand','') or r.get('main_group','')} [{r.get('unit','PCS')}]"
                                 for r in _ap_results]
                    _ap_sel_idx = st.selectbox("Select product", range(len(_ap_names)),
                                               format_func=lambda i: _ap_names[i],
                                               key=f"bo_ap_sel_{get_display_order_id(order)}")
                    _ap_row = _ap_results[_ap_sel_idx]

                    _ap_pr1, _ap_pr2 = st.columns(2)
                    _ap_price = _ap_pr1.number_input("Unit Price (₹)",
                                                      min_value=0.0, step=10.0,
                                                      key=f"bo_ap_price_{get_display_order_id(order)}")
                    _ap_gst   = _ap_pr2.number_input("GST%",
                                                      value=float(_ap_row.get("gst_percent") or 0),
                                                      min_value=0.0, max_value=28.0,
                                                      key=f"bo_ap_gst_{get_display_order_id(order)}")

                    if st.button("✅ Add to Order", key=f"bo_ap_add_{get_display_order_id(order)}",
                                 type="primary", use_container_width=True):
                        try:
                            import uuid as _apu
                            import json as _apj_save
                            from modules.sql_adapter import run_write as _rw_ap
                            _ap_eye_db = "R" if _ap_eye in ("R","Right") else                                      "L" if _ap_eye in ("L","Left") else "B"
                            _ap_line = {
                                "product_id": str(_ap_row["id"]),
                                "product_name": _ap_row.get("product_name"),
                                "brand": _ap_row.get("brand"),
                                "main_group": _ap_row.get("main_group"),
                                "eye_side": _ap_eye_db,
                                "quantity": int(_ap_qty),
                                "billing_qty": int(_ap_qty),
                                "unit_price": float(_ap_price or 0),
                                "gst_percent": float(_ap_gst or 0),
                                "lens_params": {},
                            }
                            # New backoffice product lines should pass through the
                            # same discount engine before they hit DB.
                            try:
                                from modules.pricing.discount_flow import apply_order_discounts
                                apply_order_discounts(
                                    [_ap_line],
                                    party_id=str(order.get("party_id") or ""),
                                    order_type=str(order.get("order_type") or "RETAIL").upper(),
                                )
                            except Exception as _ap_de:
                                logger.warning("Backoffice add-product discount failed: %s", _ap_de)
                            if not float(_ap_line.get("billing_total") or _ap_line.get("total_price") or 0):
                                _ap_disc = float(_ap_line.get("discount_amount") or 0)
                                _ap_net = round(max(0.0, (float(_ap_price or 0) * int(_ap_qty)) - _ap_disc), 2)
                                try:
                                    from modules.core.price_qty_governor import compute_line_gst as _ap_cgst
                                    _ap_g = _ap_cgst(
                                        _ap_net / max(int(_ap_qty), 1),
                                        int(_ap_qty),
                                        float(_ap_gst or 0),
                                        str(order.get("order_type") or "RETAIL").upper(),
                                    )
                                    _ap_line["billing_total"] = _ap_net
                                    _ap_line["total_price"] = _ap_net
                                    _ap_line["gst_amount"] = float(_ap_g.get("gst_amount") or 0)
                                except Exception:
                                    _ap_line["billing_total"] = _ap_net
                                    _ap_line["total_price"] = _ap_net
                                    _ap_line["gst_amount"] = round(_ap_net * float(_ap_gst or 0) / 100, 2)
                            _new_ap_id = str(_apu.uuid4())
                            _rw_ap("""
                                INSERT INTO order_lines
                                  (id, order_id, product_id, eye_side,
                                   unit_price, total_price, billing_total,
                                   gst_percent, gst_amount,
                                   quantity, billing_qty, allocated_qty,
                                   batch_status, lens_params,
                                   discount_percent, discount_amount, discount_rule,
                                   applied_rule_ids)
                                VALUES
                                  (%(id)s::uuid, %(oid)s::uuid, %(pid)s::uuid, %(eye)s,
                                   %(up)s, %(tp)s, %(tp)s,
                                   %(gp)s, %(ga)s,
                                   %(qty)s, %(qty)s, 0,
                                   'PENDING', %(lp)s::jsonb,
                                   %(dp)s, %(da)s, %(dr)s, %(ari)s)
                            """, {
                                "id":  _new_ap_id,
                                "oid": str(order.get("id") or ""),
                                "pid": str(_ap_row["id"]),
                                "eye": _ap_eye_db,
                                "up":  float(_ap_line.get("unit_price") or 0),
                                "tp":  float(_ap_line.get("billing_total") or _ap_line.get("total_price") or 0),
                                "gp":  float(_ap_line.get("gst_percent") or _ap_gst or 0),
                                "ga":  float(_ap_line.get("gst_amount") or 0),
                                "qty": _ap_qty,
                                "lp": _apj_save.dumps(_ap_line.get("lens_params") or {}),
                                "dp": float(_ap_line.get("discount_percent") or 0),
                                "da": float(_ap_line.get("discount_amount") or 0),
                                "dr": str(_ap_line.get("discount_rule") or ""),
                                "ari": str(_ap_line.get("applied_rule_ids") or ""),
                            })
                            try:
                                _ap_line["id"] = _new_ap_id
                                _ap_line["line_id"] = _new_ap_id
                            except Exception:
                                pass
                            try:
                                _ap_line_db = dict(_ap_line)
                                _ap_line_db["id"] = _ap_line_db.get("line_id") or str(_ap_line_db.get("id") or "")
                                _ap_line_db["line_id"] = _ap_line_db["id"]
                                order.setdefault("lines", []).append(_ap_line_db)
                                from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                                refresh_order_pricing_rules(order, persist=True)
                            except Exception as _ap_sync_err:
                                logger.warning("Backoffice add-product pricing sync failed: %s", _ap_sync_err)
                            _bo_refresh_order_total_value(str(order.get("id") or order.get("order_id") or ""))
                            try:
                                from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                                ensure_order_production_refs(order_id=str(order.get("id") or order.get("order_id") or ""))
                            except Exception as _ap_ref_err:
                                logger.warning("Backoffice add-product production_ref fill failed: %s", _ap_ref_err)
                            st.session_state.pop(f"_bo_disc_val_done_{order_id}", None)
                            st.success(f"✅ {_ap_row['product_name']} added to order")
                            st.session_state[_add_mode_key] = None
                            st.rerun()
                        except Exception as _ape2:
                            st.error(f"Failed: {_ape2}")

                st.markdown("</div>", unsafe_allow_html=True)

            elif _add_mode == "SERVICE":
                import json as _asj
                st.markdown("<div style='background:#0f172a;border:1px solid #334155;"
                            "border-top:3px solid #f59e0b;border-radius:8px;padding:12px;margin:6px 0'>",
                            unsafe_allow_html=True)
                st.markdown("**🔧 Add Service to Order**")
                _order_type_add = str(order.get("order_type","RETAIL")).upper()
                try:
                    from modules.sql_adapter import resolve_order_uuid as _resolve_order_uuid
                    _order_id_add = _resolve_order_uuid(order.get("id") or order.get("order_id") or order.get("order_no")) or ""
                except Exception:
                    _order_id_add = ""
                try:
                    from modules.backoffice.service_master import (
                        fetch_service_types as _fst_add,
                        service_price as _sp_add,
                    )
                    from modules.core.business_rules import SERVICE_CHARGE_TYPES as _SCT_add
                    _svc_rows_add = _fst_add(active_only=True)
                except Exception:
                    _svc_rows_add = []
                    _sp_add = lambda s, ot, *args, **kwargs: float((s or {}).get("default_price") or 0)
                    _SCT_add = {"COLOURING":{"label":"Colouring","icon":"🎨","default_gst":18},
                                "FITTING":{"label":"Fitting","icon":"🔧","default_gst":18},
                                "COURIER":{"label":"Courier","icon":"🚚","default_gst":18},
                                "OTHER":{"label":"Other","icon":"➕","default_gst":18},
                                "MISC":{"label":"Other","icon":"➕","default_gst":18},
                                "CONSULTATION":{"label":"Consultation","icon":"🩺","default_gst":0},
                                "EYE_TESTING":{"label":"Eye Testing","icon":"👁️","default_gst":0}}
                _svc_rows_add = _bo_complete_service_rows(_svc_rows_add)

                _svc_by_group_add = {}
                for _r in _svc_rows_add:
                    _svc_by_group_add.setdefault(str(_r.get("service_group","OTHER")).upper(), []).append(_r)

                st.markdown("🧾 **Service Charges** — Fitting · Colouring · Courier")
                st.caption("Charges are saved directly to this order. Colouring/Fitting route to production; Courier goes direct to billing.")
                _bo_svc_pick_key = f"_bo_svc_add_type_{_order_id_add[:8]}"
                _bo_svc_by_code = {
                    str(s.get("service_code") or "").upper(): s
                    for s in _svc_rows_add
                }
                if not st.session_state.get(_bo_svc_pick_key):
                    _add_group_order = ["FITTING", "COLOURING", "COURIER", "OTHER", "MISC", "CONSULTATION", "EYE_TESTING"]
                    _add_group_order += [
                        _g for _g in sorted(_svc_by_group_add)
                        if _g not in set(_add_group_order)
                    ]
                    for _grp_name in _add_group_order:
                        _grp_items = _svc_by_group_add.get(_grp_name, [])
                        if not _grp_items:
                            continue
                        _cfg = _SCT_add.get(_grp_name) or _SCT_add.get("MISC", {})
                        _icon = _cfg.get("icon", "➕")
                        with st.expander(f"{_icon} {_grp_name.title()} Services", expanded=(_grp_name in ("FITTING", "COLOURING"))):
                            _cols = st.columns(min(3, max(1, len(_grp_items))))
                            for _i, _svc_def_row in enumerate(_grp_items):
                                _code = str(_svc_def_row.get("service_code") or "").upper()
                                _svc_name = str(_svc_def_row.get("service_name") or _code or _grp_name.title())
                                _rate = float(_sp_add(_svc_def_row, _order_type_add) if _svc_def_row else 0)
                                if _rate <= 0:
                                    _rate = float(_svc_def_row.get("default_price") or 0)
                                _gst = float(_svc_def_row.get("gst_percent") or _cfg.get("default_gst", 18) or 0)
                                with _cols[_i % len(_cols)]:
                                    st.caption(f"₹{_rate:,.0f} · GST {_gst:g}%")
                                    if st.button(f"+ {_svc_name}", key=f"bo_sc_pick_{_order_id_add[:8]}_{_code}", use_container_width=True):
                                        st.session_state[_bo_svc_pick_key] = _code
                                        st.rerun()
                else:
                    _ct = str(st.session_state.get(_bo_svc_pick_key) or "").upper()
                    _svc = _bo_svc_by_code.get(_ct, {})
                    _grp = str(_svc.get("service_group") or _ct or "OTHER").upper()
                    _cfg = _SCT_add.get(_grp) or _SCT_add.get("MISC", {})
                    _lbl = _cfg.get("label", _grp.title())
                    _icon = _cfg.get("icon", "➕")
                    _default_rate = float(_sp_add(_svc, _order_type_add) if _svc else 0)
                    if _default_rate <= 0:
                        _default_rate = float(_svc.get("default_price") or 0)
                    _default_gst = float(_svc.get("gst_percent") or _cfg.get("default_gst", 18) or 0)
                    _courier_provider_id_add = ""
                    _courier_provider_name_add = ""
                    _courier_rate_option_id_add = ""
                    _courier_rate_option_label_add = ""
                    _courier_parcel_size_add = ""
                    if _grp == "COURIER":
                        try:
                            from modules.backoffice.service_master import fetch_providers as _bo_fetch_providers
                            from modules.backoffice.service_master import fetch_courier_rate_options as _bo_fetch_courier_slabs
                            _bo_couriers = _bo_fetch_providers("COURIER", active_only=True) or []
                        except Exception:
                            _bo_couriers = []
                            _bo_fetch_courier_slabs = lambda *_a, **_k: []
                        _bo_provider_ids = [""] + [str(_p.get("id") or "") for _p in _bo_couriers]
                        _bo_pref_pid = str(order.get("preferred_courier_provider_id") or "")
                        _bo_provider_idx = _bo_provider_ids.index(_bo_pref_pid) if _bo_pref_pid in _bo_provider_ids else 0

                        def _bo_fmt_courier(_pid):
                            if not _pid:
                                return "— Select Courier Provider —"
                            _p = next((_x for _x in _bo_couriers if str(_x.get("id") or "") == str(_pid)), {})
                            return str(_p.get("provider_name") or _pid)

                        _courier_provider_id_add = st.selectbox(
                            "Courier provider",
                            _bo_provider_ids,
                            index=_bo_provider_idx,
                            format_func=_bo_fmt_courier,
                            key=f"bo_sc_courier_provider_{_order_id_add[:8]}_{_ct}",
                        )
                        _bo_provider = next(
                            (_x for _x in _bo_couriers if str(_x.get("id") or "") == str(_courier_provider_id_add)),
                            {},
                        )
                        _courier_provider_name_add = str(_bo_provider.get("provider_name") or "")
                        _bo_slabs = _bo_fetch_courier_slabs(_courier_provider_id_add, active_only=True) if _courier_provider_id_add else []
                        _bo_slab_ids = [""] + [str(_s.get("id") or "") for _s in _bo_slabs]
                        if _bo_slabs:
                            _bo_lowest = min(_bo_slabs, key=lambda _s: float(_s.get("charge_base") or 0))
                            _bo_slab_default = str(_bo_lowest.get("id") or "")
                            _bo_slab_idx = _bo_slab_ids.index(_bo_slab_default) if _bo_slab_default in _bo_slab_ids else 0
                        else:
                            _bo_slab_idx = 0

                        def _bo_fmt_slab(_sid):
                            if not _sid:
                                return "Provider default / manual"
                            _s = next((_x for _x in _bo_slabs if str(_x.get("id") or "") == str(_sid)), {})
                            _code = str(_s.get("parcel_size_code") or "")
                            return (
                                f"{_s.get('option_label') or ''}"
                                + (f" · {_code}" if _code else "")
                                + f" — ₹{float(_s.get('charge_base') or 0):,.2f}"
                            )

                        _courier_rate_option_id_add = st.selectbox(
                            "Courier charge slab / parcel size",
                            _bo_slab_ids,
                            index=_bo_slab_idx,
                            format_func=_bo_fmt_slab,
                            key=f"bo_sc_courier_slab_{_order_id_add[:8]}_{_ct}",
                        )
                        _bo_slab = next(
                            (_x for _x in _bo_slabs if str(_x.get("id") or "") == str(_courier_rate_option_id_add)),
                            {},
                        )
                        if _bo_slab:
                            _default_rate = float(_bo_slab.get("charge_base") or _default_rate or 0)
                            _default_gst = float(_bo_slab.get("gst_percent") or _default_gst or 18)
                            _courier_rate_option_label_add = str(_bo_slab.get("option_label") or "")
                            _courier_parcel_size_add = str(_bo_slab.get("parcel_size_code") or "")
                            st.caption("Lowest courier slab is auto-selected. Change dropdown if parcel is bigger.")
                    st.markdown(f"**{_icon} Add {_lbl}**")
                    _c1, _c2, _cq, _c3 = st.columns([3, 1.3, 1.2, 1.1])
                    with _c1:
                        _desc = st.text_input(
                            "Description",
                            value=str(_svc.get("service_name") or _lbl),
                            key=f"bo_sc_desc_{_order_id_add[:8]}_{_ct}",
                        )
                    with _c2:
                        _rate_pair = st.number_input(
                            "₹ Rate / pair",
                            min_value=0.0,
                            value=float(_default_rate or 0),
                            step=10.0,
                            key=f"bo_sc_rate_{_order_id_add[:8]}_{_ct}",
                        )
                    with _cq:
                        _qty_factor = st.selectbox(
                            "Qty",
                            [0.5, 1.0, 1.5, 2.0, 3.0],
                            index=1,
                            format_func=lambda v: (
                                f"{v:g} pair — 1 eye" if v == 0.5 else
                                f"{v:g} pair — both eyes" if v == 1.0 else
                                f"{v:g} pair"
                            ),
                            key=f"bo_sc_qty_{_order_id_add[:8]}_{_ct}",
                        )
                    with _c3:
                        _gst = st.number_input(
                            "GST %",
                            min_value=0.0,
                            max_value=28.0,
                            value=_default_gst,
                            step=0.5,
                            key=f"bo_sc_gst_{_order_id_add[:8]}_{_ct}",
                        )

                    _instr = ""
                    _photo_b64 = ""
                    _photo_name = ""
                    if _grp in ("COLOURING", "FITTING"):
                        _instr = st.text_area(
                            "Special instruction for production / provider",
                            placeholder="Tint shade, sample reference, fitting note, urgency...",
                            key=f"bo_sc_instr_{_order_id_add[:8]}_{_ct}",
                            height=70,
                        )
                        if _grp == "COLOURING":
                            _photo = st.file_uploader(
                                "Colour sample photograph",
                                type=["jpg", "jpeg", "png", "webp"],
                                key=f"bo_sc_colour_sample_{_order_id_add[:8]}_{_ct}",
                            )
                            if _photo:
                                import base64 as _bo_sc_b64
                                _photo_b64 = _bo_sc_b64.b64encode(_photo.read()).decode("ascii")
                                _photo_name = _photo.name

                    _b1, _b2 = st.columns(2)
                    if _b1.button(f"✅ Add {_qty_factor:g} pair", type="primary",
                                  key=f"bo_sc_confirm_{_order_id_add[:8]}_{_ct}",
                                  use_container_width=True):
                        if not _order_id_add:
                            st.error("Order reference missing. Reopen the order and try again.")
                            st.stop()
                        try:
                            import uuid as _asu
                            from modules.sql_adapter import run_write as _rw_as
                            _base_amt = round(float(_rate_pair or 0) * float(_qty_factor or 1), 2)
                            _direct = (_grp not in ("COLOURING", "FITTING"))
                            _svc_route2 = _service_route_for_group(_grp, _direct)
                            _tot2, _ga2 = _bo_service_line_amounts(_base_amt, _gst, order.get("order_type"))
                            _svc_product_label = f"{_lbl}: {_desc}"
                            _svc_pid2 = _ensure_bo_service_product(_grp, _svc_product_label, _gst)
                            _dup_rows = []
                            try:
                                from modules.sql_adapter import run_query as _rq_dup_svc
                                _dup_rows = _rq_dup_svc("""
                                    SELECT id::text
                                    FROM order_lines
                                    WHERE order_id=%(oid)s::uuid
                                      AND COALESCE(is_deleted,FALSE)=FALSE
                                      AND COALESCE(is_service_line,FALSE)=TRUE
                                      AND UPPER(COALESCE(lens_params->>'service_code','')) = UPPER(%(code)s)
                                    LIMIT 1
                                """, {
                                    "oid": _order_id_add,
                                    "code": str(_svc.get("service_code") or _ct).upper(),
                                }) or []
                            except Exception:
                                _dup_rows = []
                            _dup_key = f"bo_dup_service_add_{_order_id_add[:8]}_{_ct}"
                            if _dup_rows and not st.session_state.get(_dup_key):
                                st.warning(f"{_lbl}: {_desc} already exists on this order. Click Add again to confirm duplicate.")
                                st.session_state[_dup_key] = True
                                st.stop()
                            _lp2  = _asj.dumps({
                                "charge_type": _grp,
                                "service_type": _grp,
                                "service_code": str(_svc.get("service_code") or _ct).upper(),
                                "service_description": _desc,
                                "service_display_name": _svc_product_label,
                                "display_product_name": _svc_product_label,
                                "service_production_type": "" if _direct else _grp,
                                "manufacturing_route": _svc_route2,
                                "service_instruction": _instr,
                                "colour_sample_photo": _photo_b64,
                                "colour_sample_filename": _photo_name,
                                "service_qty_factor": float(_qty_factor),
                                "service_rate_per_pair": float(_rate_pair or 0),
                                "courier_provider_id": _courier_provider_id_add,
                                "courier_provider_name": _courier_provider_name_add,
                                "courier_rate_option_id": _courier_rate_option_id_add,
                                "courier_rate_option_label": _courier_rate_option_label_add,
                                "courier_parcel_size": _courier_parcel_size_add,
                                "service_origin": "backoffice_add",
                            })
                            _rw_as("""
                                INSERT INTO order_lines
                                  (id, order_id, product_id, eye_side,
                                   unit_price, total_price, billing_total,
                                   gst_percent, gst_amount,
                                   quantity, billing_qty, allocated_qty,
                                   is_service_line, batch_status, lens_params)
                                VALUES
                                  (%(id)s::uuid, %(oid)s::uuid, %(pid)s::uuid, 'S',
                                   %(up)s, %(tp)s, %(tp)s,
                                   %(gp)s, %(ga)s,
                                   1, 1, %(aq)s,
                                   TRUE, %(bs)s, %(lp)s::jsonb)
                            """, {
                                "id":  str(_asu.uuid4()),
                                "oid": _order_id_add,
                                "pid": _svc_pid2,
                                "up":  _base_amt, "tp": _tot2,
                                "gp":  _gst, "ga": _ga2,
                                "aq":  1 if _direct else 0,
                                "bs":  "READY" if _direct else "PENDING",
                                "lp":  _lp2,
                            })
                            _bo_refresh_order_total_value(_order_id_add)
                            try:
                                from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                                ensure_order_production_refs(order_id=_order_id_add)
                            except Exception as _as_ref_err:
                                logger.warning("Backoffice add-service production_ref fill failed: %s", _as_ref_err)
                            st.session_state.pop(_dup_key, None)
                            st.success(f"✅ {_lbl}: {_desc} — {_qty_factor:g} pair ₹{_tot2:.0f}")
                            st.session_state.pop(_bo_svc_pick_key, None)
                            st.session_state[_add_mode_key] = None
                            st.rerun()
                        except Exception as _ase:
                            st.error(f"Failed: {_ase}")
                    if _b2.button("✕ Cancel", key=f"bo_sc_cancel_{_order_id_add[:8]}_{_ct}",
                                  use_container_width=True):
                        st.session_state.pop(_bo_svc_pick_key, None)
                        st.rerun()

                for _svc_t in []:
                    _cfg   = _SCT_add.get(_svc_t, {})
                    _lbl   = _cfg.get("label", _svc_t.title())
                    _icon  = _cfg.get("icon","🔧")
                    _gst_d = float(_cfg.get("default_gst",18))
                    _grow  = _svc_by_group_add.get(_svc_t, [])
                    _gopts = ["None"] + [r.get("service_name","") for r in _grow]
                    _gsel  = st.selectbox(f"{_icon} {_lbl}", _gopts,
                                          key=f"bo_as_{_svc_t}_{_order_id_add[:8]}")
                    if _gsel and _gsel != "None":
                        _grow_def = next((r for r in _grow if r.get("service_name")==_gsel), {})
                        _gdamt = float(_sp_add(_grow_def, _order_type_add) if _grow_def else 0)

                        # ── Mandatory qty confirmation ─────────────────────────
                        _qk  = f"bo_as_q_{_svc_t}_{_order_id_add[:8]}"
                        _qok = st.session_state.get(_qk + "_ok", False)
                        _gqty = st.selectbox(
                            f"👁 {_lbl} Qty — confirm before price",
                            [0.5, 1.0, 1.5, 2.0],
                            index=1,
                            format_func=lambda v: (
                                f"{v:g} pair — 1 eye only" if v == 0.5 else
                                f"{v:g} pair — both eyes" if v == 1.0 else
                                f"{v:g} pair"
                            ),
                            key=_qk,
                        )
                        if not _qok:
                            if st.button(f"✅ Confirm {_gqty:g} pair",
                                         key=f"bo_as_qc_{_svc_t}_{_order_id_add[:8]}",
                                         type="primary", use_container_width=True):
                                st.session_state[_qk + "_ok"] = True
                                st.rerun()
                            st.caption("⚠️ Confirm qty to unlock price entry")
                        else:
                            st.info(f"✅ {_gqty:g} pair confirmed")
                            if st.button("↩️ Change", key=f"bo_as_qr_{_svc_t}_{_order_id_add[:8]}"):
                                st.session_state[_qk + "_ok"] = False
                                st.rerun()
                            _gc1, _gc2 = st.columns(2)
                            _gamt  = _gc1.number_input(f"Amount ₹", min_value=0.0,
                                                       value=round(_gdamt * _gqty, 2), step=10.0,
                                                       key=f"bo_as_amt_{_svc_t}_{_order_id_add[:8]}_{_gsel}")
                            _ggst  = _gc2.number_input("GST%", value=_gst_d, min_value=0.0, max_value=28.0,
                                                       key=f"bo_as_gst_{_svc_t}_{_order_id_add[:8]}")
                            _ginst = st.text_input("Instruction",
                                                   key=f"bo_as_ins_{_svc_t}_{_order_id_add[:8]}",
                                                   placeholder="Special instruction for provider...")
                            if st.button(f"➕ Add {_lbl} — {_gqty:g} pair",
                                         key=f"bo_as_add_{_svc_t}_{_order_id_add[:8]}",
                                         use_container_width=True, type="primary"):
                                if _gamt >= 0 and _order_id_add:
                                    try:
                                        import uuid as _asu
                                        from modules.sql_adapter import run_write as _rw_as
                                        _direct = (_svc_t == "COURIER")
                                        _svc_route2 = _service_route_for_group(_svc_t, _direct)
                                        _ga2 = 0.0
                                        _tot2, _ga2 = _bo_service_line_amounts(_gamt, _ggst, order.get("order_type"))
                                        _svc_product_label = f"{_lbl}: {_gsel}"
                                        _svc_pid2 = _ensure_bo_service_product(_svc_t, _svc_product_label, _ggst)
                                        _lp2  = _asj.dumps({
                                            "charge_type": _svc_t,
                                            "service_type": _svc_t,
                                            "service_description": _gsel,
                                            "service_display_name": _svc_product_label,
                                            "display_product_name": _svc_product_label,
                                            "service_production_type": "" if _direct else _svc_t,
                                            "manufacturing_route": _svc_route2,
                                            "service_instruction": _ginst,
                                            "service_qty_factor": float(_gqty),
                                            "service_rate_per_pair": float(_gdamt or 0),
                                            "service_origin": "backoffice_add",
                                        })
                                        _rw_as("""
                                            INSERT INTO order_lines
                                              (id, order_id, product_id, eye_side,
                                               unit_price, total_price, billing_total,
                                               gst_percent, gst_amount,
                                               quantity, billing_qty, allocated_qty,
                                               is_service_line, batch_status, lens_params)
                                            VALUES
                                              (%(id)s::uuid, %(oid)s::uuid, %(pid)s::uuid, 'S',
                                               %(up)s, %(tp)s, %(tp)s,
                                               %(gp)s, %(ga)s,
                                               1, 1, %(aq)s,
                                               TRUE, %(bs)s, %(lp)s::jsonb)
                                        """, {
                                            "id":  str(_asu.uuid4()),
                                            "oid": _order_id_add,
                                            "pid": _svc_pid2,
                                            "up":  _gamt, "tp": _tot2,
                                            "gp":  _ggst, "ga": _ga2,
                                            "aq":  1 if _direct else 0,
                                            "bs":  "READY" if _direct else "PENDING",
                                            "lp":  _lp2,
                                        })
                                        st.success(f"✅ {_lbl}: {_gsel} — {_gqty:g} pair ₹{_tot2:.0f}")
                                        st.session_state[_qk + "_ok"] = False
                                        st.session_state[_add_mode_key] = None
                                        st.rerun()
                                    except Exception as _ase:
                                        st.error(f"Failed: {_ase}")
                                else:
                                    st.warning("Select a service first")
                st.markdown("</div>", unsafe_allow_html=True)

            #  RENDER ALLOCATION WINDOW IF ACTIVE
            if st.session_state.get('bo_show_allocation_window', False):
                line_idx = st.session_state.get('bo_allocation_line_idx')
            
                if line_idx is not None and line_idx < len(all_lines):
                    line = all_lines[line_idx]
                    render_allocation_window(line, line_idx, order)

            # ========== FINAL SAVE TO ORDER ==========
            st.markdown("---")
            st.markdown("###  Order Summary")

            # ── Sticky restore banner: surface soft-deleted lines at the top ──
            # The detail view's existing "↩ Restore Deleted Lines" expander is
            # far below; users couldn't see at a glance that a delete had taken
            # effect. This banner shows a count + a one-click jump down to the
            # restore panel (it lives at the existing expander; we just hint).
            try:
                _deleted_count = sum(
                    1 for _l in all_lines if bool(_l.get("is_deleted"))
                )
                if _deleted_count > 0:
                    st.warning(
                        f"🗑 {_deleted_count} line(s) deleted from this order. "
                        "Scroll down to **↩ Restore Deleted Lines** to undo."
                    )
            except Exception:
                pass

            if st.button(" System Health Check", use_container_width=True):

                issues = run_system_health_check(order)

                if not issues:
                    st.success(" System OK. No issues found.")
                else:
                    st.error(" Issues Found:")
                    for i in issues:
                        st.write("", i)
        
            # Calculate order totals with R/L breakdown
            # FIX: exclude soft-deleted lines from every count and bucket below.
            # Without this, a service that was just deleted via 🗑 still appears
            # in the Order Summary because `all_lines` carries it (it remains in
            # the in-memory list with is_deleted=True until next reload). Other
            # parts of this file already filter is_deleted (e.g. lines 690/1521/
            # 1916) — the Order Summary section was missed.
            _visible_lines = [
                _l for _l in all_lines if not bool(_l.get("is_deleted"))
            ]
            total_items = len(_visible_lines)
        
            # Separate R and L lines - handle both short and full eye_side formats
            for _sum_guard_line in _visible_lines:
                _bo_enforce_wholesale_price(_sum_guard_line, order.get("order_type"))
            r_lines = [line for line in _visible_lines if line.get('eye_side', '').upper() in ['R', 'RIGHT']]
            l_lines = [line for line in _visible_lines if line.get('eye_side', '').upper() in ['L', 'LEFT']]
            other_lines = [line for line in _visible_lines if line.get('eye_side', '').upper() not in ['R', 'RIGHT', 'L', 'LEFT']]
        
            # Calculate R eye totals
            _order_type_calc = str(order.get("order_type") or "WHOLESALE").upper()
            r_billing = sum(_bo_line_amounts(line, _order_type_calc)["grand"] for line in r_lines)
            r_discount = sum(_bo_float(line.get('discount_amount')) for line in r_lines)
        
            # Calculate L eye totals
            l_billing = sum(_bo_line_amounts(line, _order_type_calc)["grand"] for line in l_lines)
            l_discount = sum(_bo_float(line.get('discount_amount')) for line in l_lines)
        
            # Calculate other items totals
            other_billing = sum(_bo_line_amounts(line, _order_type_calc)["grand"] for line in other_lines)
            other_discount = sum(_bo_float(line.get('discount_amount')) for line in other_lines)
        
            # Grand totals
            total_billing = r_billing + l_billing + other_billing
            total_discount = r_discount + l_discount + other_discount
        
            # Summary metrics row
            col_total1, col_total2, col_total3 = st.columns(3)
            with col_total1:
                st.metric("Total Items", total_items)
            with col_total2:
                st.metric("Total Discount", f"{total_discount:.2f}")
            with col_total3:
                st.metric("**Final Amount**", f"**{total_billing:.2f}**")
        
            # Detailed R/L Breakdown - Compact View
            st.markdown("---")
        
            # Right Eye Block
            with st.container(border=True):
                st.markdown("####  Right Eye")
                st.caption(f"{len(r_lines)} items | Subtotal: {r_billing:.2f}")
            
                if r_lines:
                    # Header row
                    hcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                    hcols[0].caption("Product")
                    hcols[1].caption("Qty")
                    hcols[2].caption("Unit Price")
                    hcols[3].caption("Discount")
                    hcols[4].caption("GST%")
                    hcols[5].caption("GST Amt")
                    hcols[6].caption("Total")
                    hcols[7].caption("")
                    st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                    for idx, line in enumerate(r_lines, 1):
                        _bo_enforce_wholesale_price(line, order.get("order_type"))
                        qty_disp = _bo_qty_display(line)
                        # ── GST fields ──
                        gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                        _amt_line = _bo_line_amounts(line, _order_type_calc)
                        gst_amt     = _amt_line["gst"]
                        tax_inc     = line.get('tax_inclusive', True)
                        gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                        disc_pct    = float(line.get('discount_percent') or 0)
                        unit_price  = float(line.get('unit_price') or 0)
                        total       = _amt_line["grand"]
                        lcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                        lcols[0].write(f"{idx}. {_bo_line_display_name(line)}")
                        lcols[1].write(qty_disp)
                        lcols[2].write(f"₹{unit_price:,.2f}")
                        lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                        lcols[4].write(gst_label)
                        lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                        lcols[6].write(f"₹{total:,.2f}")
                        _bo_summary_row_delete_button(line, order, lcols[7])
                else:
                    st.caption("No Right Eye items")
        
            # Left Eye Block
            with st.container(border=True):
                st.markdown("####  Left Eye")
                st.caption(f"{len(l_lines)} items | Subtotal: {l_billing:.2f}")
            
                if l_lines:
                    # Header row
                    hcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                    hcols[0].caption("Product")
                    hcols[1].caption("Qty")
                    hcols[2].caption("Unit Price")
                    hcols[3].caption("Discount")
                    hcols[4].caption("GST%")
                    hcols[5].caption("GST Amt")
                    hcols[6].caption("Total")
                    hcols[7].caption("")
                    st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                    for idx, line in enumerate(l_lines, 1):
                        _bo_enforce_wholesale_price(line, order.get("order_type"))
                        qty_disp = _bo_qty_display(line)
                        gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                        _amt_line = _bo_line_amounts(line, _order_type_calc)
                        gst_amt     = _amt_line["gst"]
                        tax_inc     = line.get('tax_inclusive', True)
                        gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                        disc_pct    = float(line.get('discount_percent') or 0)
                        unit_price  = float(line.get('unit_price') or 0)
                        total       = _amt_line["grand"]
                        lcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                        lcols[0].write(f"{idx}. {_bo_line_display_name(line)}")
                        lcols[1].write(qty_disp)
                        lcols[2].write(f"₹{unit_price:,.2f}")
                        lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                        lcols[4].write(gst_label)
                        lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                        lcols[6].write(f"₹{total:,.2f}")
                        _bo_summary_row_delete_button(line, order, lcols[7])
                else:
                    st.caption("No Left Eye items")
        
            # Other Items Section - Compact (if any)
            if other_lines:
                with st.container(border=True):
                    st.markdown("####  Other Items")
                    st.caption(f"{len(other_lines)} items | Subtotal: {other_billing:.2f}")
                
                    # Header row
                    hcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                    hcols[0].caption("Product")
                    hcols[1].caption("Qty")
                    hcols[2].caption("Unit Price")
                    hcols[3].caption("Discount")
                    hcols[4].caption("GST%")
                    hcols[5].caption("GST Amt")
                    hcols[6].caption("Total")
                    hcols[7].caption("")
                    st.markdown("<hr style='margin:2px 0 6px 0'>", unsafe_allow_html=True)
                    for idx, line in enumerate(other_lines, 1):
                        _bo_enforce_wholesale_price(line, order.get("order_type"))
                        qty_disp = _bo_qty_display(line)
                        gst_pct     = float(line.get('gst_percent_used') or line.get('gst_percent') or 0)
                        _amt_line = _bo_line_amounts(line, _order_type_calc)
                        gst_amt     = _amt_line["gst"]
                        tax_inc     = line.get('tax_inclusive', True)
                        gst_label   = f"{gst_pct:.0f}%" + (" (incl)" if tax_inc else " (+)") if gst_pct else "⚠️ Not set"
                        disc_pct    = float(line.get('discount_percent') or 0)
                        unit_price  = float(line.get('unit_price') or 0)
                        total       = _amt_line["grand"]
                        lcols = st.columns([3, 2, 1, 1, 1, 1, 1, 0.6])
                        lcols[0].write(f"{idx}. {_bo_line_display_name(line)}")
                        lcols[1].write(qty_disp)
                        lcols[2].write(f"₹{unit_price:,.2f}")
                        lcols[3].write(f"{disc_pct:.1f}%" if disc_pct else "—")
                        lcols[4].write(gst_label)
                        lcols[5].write(f"₹{gst_amt:,.2f}" if gst_amt else ("⚠️" if gst_pct else "—"))
                        lcols[6].write(f"₹{total:,.2f}")
                        _bo_summary_row_delete_button(line, order, lcols[7])
        
            st.markdown("---")

            # ── Debug Overlay (only when debug_pricing enabled in sidebar) ──────────
            if st.session_state.get("debug_pricing"):
                try:
                    from .debug_pricing_overlay import render_debug_overlay
                    render_debug_overlay(order, all_lines)
                except Exception as _dbg:
                    st.caption(f"Debug overlay error: {_dbg}")
            # ──────────────────────────────────────────────────────────────────────────

            # ══════════════════════════════════════════════════════════════════════
            # 🎯 SUPPLIER / JOB ASSIGNMENT PANEL
            # Blocked once a blank is assigned (job card saved) or challan exists.
            # ══════════════════════════════════════════════════════════════════════
            _order_status_upper = str(order.get("status","")).upper()
            _pipeline_locked_statuses = {
                "IN_PRODUCTION","READY","CHALLANED","INVOICED","DISPATCHED","DELIVERED","CLOSED"
            }
            # Check if any challan exists for this order
            _has_challan_lock = False
            try:
                # WIN 2: read from session_state cache populated by the tab1 check above.
                # If not cached yet (edge case), fall back to a live query.
                _ch_cache_key2 = f"_bo_challan_exists_{order_id}"
                if _ch_cache_key2 in st.session_state:
                    _has_challan_lock = st.session_state[_ch_cache_key2]
                else:
                    from modules.sql_adapter import run_query as _rq_chk
                    _ch_rows = _rq_chk("""
                        SELECT 1 FROM challans c
                        WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                            OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                          AND c.status NOT IN ('CANCELLED','VOID')
                        LIMIT 1
                    """, {"oid": str(order.get("id") or ""), "ono": str(order.get("order_no") or "")})
                    _has_challan_lock = bool(_ch_rows)
                    st.session_state[_ch_cache_key2] = _has_challan_lock
            except Exception:
                pass

            # Check if ANY non-service lines exist — if only service lines, don't freeze
            _non_svc_lines = [l for l in all_lines
                              if not l.get("is_service_line")
                              and str(l.get("eye_side","")).upper() not in ("S","SERVICE")]
            _only_services = len(_non_svc_lines) == 0 and len(all_lines) > 0
            _order_items_frozen = (
                not _only_services  # service-only orders never freeze
                and (
                    _order_status_upper in _pipeline_locked_statuses
                    or _has_challan_lock
                    or _tab1_edit_locked
                )
            )

            if _order_items_frozen:
                if _has_challan_lock:
                    st.markdown(
                        "<div style='background:#1a0a00;border:1px solid #f97316;"
                        "border-radius:8px;padding:8px 14px;margin:8px 0'>"
                        "<span style='color:#fb923c;font-weight:700;font-size:0.82rem'>"
                        "🔒 Challan exists — saving changes is blocked.</span>"
                        "<span style='color:#78350f;font-size:0.75rem;display:block;margin-top:2px'>"
                        "To modify: cancel the challan from Billing Summary first.</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                elif _tab1_edit_locked:
                    st.markdown(
                        "<div style='background:#1a0a00;border:1px solid #f97316;"
                        "border-radius:8px;padding:8px 14px;margin:8px 0'>"
                        "<span style='color:#fb923c;font-weight:700;font-size:0.82rem'>"
                        "🔒 Supplier/external order already sent — saving changes is blocked.</span>"
                        "<span style='color:#fbbf24;font-size:0.75rem;display:block;margin-top:2px'>"
                        "Set the order back from Production before editing product, power, quantity, or route.</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    with st.expander("🔧 Supervisor: Set Back from Supplier/External", expanded=False):
                        st.caption(
                            "Use only when the line was routed/assigned but no real supplier purchase has happened. "
                            "This clears stale supplier stage/order references and makes product, power and route editable again."
                        )
                        _sb_confirm = st.checkbox(
                            "I confirm this supplier/external order was not actually sent or purchased",
                            key=f"bo_sup_setback_confirm_{order.get('id','')}",
                        )
                        if st.button(
                            "↩ Set Back to Editable",
                            key=f"bo_sup_setback_btn_{order.get('id','')}",
                            use_container_width=True,
                            disabled=not _sb_confirm,
                        ):
                            try:
                                from modules.sql_adapter import run_write as _bo_sb_write
                                _line_ids_sb = [
                                    str(_l.get("id") or _l.get("line_id") or "")
                                    for _l in (all_lines or [])
                                    if str(_l.get("manufacturing_route") or "").upper() in ("VENDOR", "EXTERNAL_LAB")
                                    or str((_l.get("lens_params") or {}).get("manufacturing_route") if isinstance(_l.get("lens_params"), dict) else "").upper() in ("VENDOR", "EXTERNAL_LAB")
                                ]
                                _line_ids_sb = [x for x in _line_ids_sb if x]
                                if not _line_ids_sb:
                                    st.warning("No supplier/external lines found to set back.")
                                else:
                                    _bo_sb_write(
                                        """
                                        UPDATE order_lines
                                           SET lens_params = COALESCE(lens_params, '{}'::jsonb)
                                                - ARRAY[
                                                    'supplier_stage','external_lab_stage',
                                                    'supplier_order_no','supplier_confirmation_no',
                                                    'supplier_order_id','purchase_order_id',
                                                    'vendor_order_ref','lab_order_ref',
                                                    'po_number','dispatch_eta'
                                                  ],
                                               updated_at = NOW()
                                         WHERE id = ANY(%(line_ids)s::uuid[])
                                        """,
                                        {"line_ids": _line_ids_sb},
                                    )
                                    st.success("Set back done. Refreshing order...")
                                    st.rerun()
                            except Exception as _sb_e:
                                st.error(f"Set back failed: {_sb_e}")
                else:
                    try:
                        from modules.backoffice.guidance import render_stage_guidance
                        render_stage_guidance(_order_status_upper, compact=True)
                    except ImportError:
                        st.info(f"ℹ️ Order is {_order_status_upper} — route/product edits read-only. Power corrections via Supervisor Override.")
            if _ASSIGNMENT_PANEL_AVAILABLE and not _tab1_edit_locked:
                render_assignment_panel(order, all_lines)
            elif _ASSIGNMENT_PANEL_AVAILABLE and _tab1_edit_locked:
                st.caption("🎯 Supplier / Job assignment is locked for this order stage.")
            # ══════════════════════════════════════════════════════════════════════

            # ── GST Verification Footer ───────────────────────────────────────────
            order_type  = order.get("order_type", "RETAIL")
            # FIX: exclude soft-deleted lines from the verification sums. Same
            # class of bug as Order Summary — without this filter a deleted
            # service still adds to GST/taxable and the displayed Grand Total
            # diverges from the actual order value (also stalls the post-save
            # gate because Grand Total ≠ stored orders.total_value).
            _verif_lines = [_l for _l in all_lines if not bool(_l.get("is_deleted"))]
            _verif_amounts = [_bo_line_amounts(_l, order_type) for _l in _verif_lines]
            gst_total   = sum(_a["gst"] for _a in _verif_amounts)
            taxable_val = sum(_a["taxable"] for _a in _verif_amounts)
            grand_val   = sum(_a["grand"] for _a in _verif_amounts)

            with st.container(border=True):
                st.caption("📊 Billing Verification Summary")
                vc = st.columns(4)
                if order_type == "RETAIL":
                    vc[0].metric("MRP Total (incl. GST)",  f"₹{grand_val:,.2f}")
                    vc[1].metric("GST Extracted",           f"₹{gst_total:,.2f}",   help="GST back-calculated from MRP")
                    vc[2].metric("Taxable Value",           f"₹{taxable_val:,.2f}")
                    vc[3].metric("Patient Pays",            f"₹{grand_val:,.2f}")
                else:
                    vc[0].metric("Subtotal (excl. GST)",   f"₹{taxable_val:,.2f}")
                    vc[1].metric("GST Added",               f"₹{gst_total:,.2f}",   help="GST added on top of selling price")
                    vc[2].metric("Grand Total",             f"₹{grand_val:,.2f}")
                    vc[3].metric("Order Type",              order_type)
                st.caption(
                    f"Source: {order.get('order_source', order_type)}  |  "                f"Tax treatment: {'GST inclusive in price' if order_type == 'RETAIL' else 'GST exclusive — added on top'}"            )
            # ─────────────────────────────────────────────────────────────────────

            from modules.utils.submit_guard import is_locked, guarded_submit
            _post_save_actions_rendered = False
            if _order_items_frozen:
                st.button(
                    "🔒 SAVE LOCKED — Order in pipeline or challan exists",
                    type="secondary", use_container_width=True,
                    key="final_save_frozen", disabled=True,
                    help="Cancel challan or use supervisor override to edit"
                )
            elif st.button(" SAVE TO ORDER", type="primary", use_container_width=True,
                         key="final_save_order", disabled=is_locked("final_save")):
                with guarded_submit("final_save") as _allowed:
                    if not _allowed:
                        st.stop()
                    # 🔐 Billing guard — never save with zero billing
                    if total_billing <= 0:
                        st.error("❌ Billing total invalid. Cannot save with zero or negative billing.")
                        st.warning("Please check line items and pricing before saving.")
                        return   # guarded_submit __exit__ clears lock

                    # 🎯 Assignment guard — uses smart auto-confirm logic
                    try:
                        from modules.backoffice.decision_engine import is_assignment_confirmed as _iac
                        _assign_ok = _iac(st.session_state, all_lines)
                    except Exception:
                        _assign_ok = st.session_state.get("bo_assignments_locked", False)
                    if _ASSIGNMENT_PANEL_AVAILABLE and not _assign_ok:
                        st.warning(
                            "⚠️ Supplier / Job assignments not confirmed. "
                            "Scroll up and click **Confirm All Assignments** before saving."
                        )
                        return

                    try:
                        # ── GST Recalculation — MUST succeed before save ──────────
                        try:
                            from modules.pricing.tax_engine import apply_taxes
                            tax_input = {
                                "order_type": order.get("order_type", "RETAIL"),
                                "net_value":  sum(
                                    float(l.get("billing_total") or l.get("total_price") or 0)
                                    for l in all_lines
                                ),
                                "lines": all_lines,
                            }
                            taxed = apply_taxes(tax_input)
                            order["tax_amount"]  = taxed["tax_amount"]
                            order["final_value"] = taxed["final_value"]
                        except Exception as _tax_err:
                            st.error(f"❌ GST recalculation failed — order NOT saved: {_tax_err}")
                            st.stop()   # lock auto-clears
                        # ─────────────────────────────────────────────────────────

                        from modules.persistence.order_persistence import save_order_to_db
                        from modules.sql_adapter import run_query, run_write

                        _old_status_for_history = order.get("status") or "PENDING"

                        # ── Smart status advance on save ─────────────────────────
                        # Once Backoffice assignments are confirmed, the order itself is
                        # confirmed. Supplier/stock/in-house readiness is still controlled
                        # line-wise by the route pipeline and billing gate.
                        _cur_status = order.get("status", "PENDING")
                        if _cur_status in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL", "UNDER_REVIEW", ""):
                            order["status"] = "CONFIRMED"
                        # ─────────────────────────────────────────────────────────

                        # ── Decide final status BEFORE save ──────────────────
                        # Use batch_status + allocation as the ground truth
                        # (manufacturing_route may be None on old orders)
                        _alloc_total  = sum(int(l.get("allocated_qty") or 0) for l in all_lines)
                        _bill_total   = sum(int(l.get("billing_qty") or 0) for l in all_lines)
                        _cur_status = order.get("status") or "PENDING"
                        if _cur_status in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL", "UNDER_REVIEW", ""):
                            order["status"] = "CONFIRMED"

                        # Write status to DB directly FIRST (before save, so upsert picks it up)
                        _new_status = order["status"]
                        try:
                            run_query(
                                "UPDATE orders SET status=%(s)s, updated_at=NOW() WHERE order_no=%(n)s",
                                {"s": _new_status, "n": order.get("order_no")},
                            )
                            if _new_status == "CONFIRMED":
                                try:
                                    run_write("""
                                        INSERT INTO order_status_history
                                            (order_id, from_status, to_status,
                                             changed_at, changed_by_name, remarks)
                                        SELECT id, %(frm)s, 'CONFIRMED',
                                               NOW(), %(by)s, %(rmk)s
                                        FROM orders
                                        WHERE order_no = %(ono)s
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM order_status_history h
                                              WHERE h.order_id = orders.id
                                                AND h.to_status = 'CONFIRMED'
                                          )
                                    """, {
                                        "frm": _old_status_for_history,
                                        "by": "Backoffice",
                                        "rmk": "Backoffice assignments confirmed and order saved",
                                        "ono": order.get("order_no"),
                                    })
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # ─────────────────────────────────────────────────────

                        saved_id = save_order_to_db(order)

                        # WIN 2: invalidate challan cache — save may affect challan state
                        try:
                            st.session_state.pop(f"_bo_challan_exists_{order_id}", None)
                        except Exception:
                            pass

                        # F5: Re-read orders.total_value from DB after save so
                        # WhatsApp message always shows the post-save amount, not
                        # the pre-save in-memory total_billing which may differ if
                        # save_order_to_db adjusted any line values.
                        try:
                            from modules.sql_adapter import run_query as _rq_tv
                            _tv_rows = _rq_tv(
                                "SELECT total_value FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
                                {"oid": str(saved_id or order.get("id") or "")},
                            ) or []
                            if _tv_rows:
                                total_billing = float(_tv_rows[0].get("total_value") or total_billing)
                        except Exception as _tv_err:
                            logger.debug("Post-save total_value re-read failed (non-fatal): %s", _tv_err)
                        try:
                            from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                            ensure_order_production_refs(
                                order_no=str(order.get("order_no") or ""),
                                order_id=str(saved_id or order.get("id") or order_id or ""),
                            )
                        except Exception as _prod_ref_err:
                            logger.warning("Backoffice save production_ref fill failed: %s", _prod_ref_err)
                        try:
                            from modules.procurement.procurement_ledger import ensure_queue_items
                            _proc_line_ids = [
                                str(l.get("line_id") or l.get("id") or "")
                                for l in all_lines
                                if not l.get("is_service_line")
                                and str(l.get("eye_side") or "").upper() not in ("S", "SERVICE")
                                and str(l.get("manufacturing_route") or "STOCK").upper() in ("STOCK", "VENDOR", "EXTERNAL_LAB")
                            ]
                            ensure_queue_items(_proc_line_ids, source="BACKOFFICE_SAVE")
                        except Exception:
                            pass
                        for line in all_lines:
                            line["pricing_locked"] = True

                        # Update the in-memory order in bo_active_orders so the
                        # detail view reflects the new status without a full reload
                        _ono = order.get("order_no")
                        for _o in st.session_state.get("bo_active_orders", []):
                            if _o.get("order_no") == _ono:
                                _o["status"]     = _new_status
                                _o["updated_at"] = str(datetime.datetime.now())[:16]
                                break

                        # Clear load cache so next dashboard Load gets fresh data
                        try:
                            from modules.backoffice.backoffice_helpers import load_orders_from_database
                            load_orders_from_database.clear()
                        except Exception:
                            pass

                        _disp_no = order.get("display_order_no") or order.get("order_no", saved_id)
                        st.success(
                            f"✅ Order **{_disp_no}** saved — "
                            f"Status: **{_new_status}** 🎯"
                        )
                        if _new_status == "CONFIRMED":
                            st.info("📦 All lines stock-allocated. Order is Collected ✓")
                        try:
                            from modules.post_save_actions import render_post_save_actions
                            render_post_save_actions(
                                order_no=str(order.get("order_no") or _disp_no),
                                party_name=str(order.get("party_name") or order.get("patient_name") or "Customer"),
                                mobile=str(order.get("mobile") or order.get("patient_mobile") or order.get("party_mobile") or ""),
                                total=float(total_billing or 0),
                                order_type=str(order.get("order_type") or "RETAIL"),
                                advance=float(order.get("advance_amount") or order.get("paid_amount") or 0),
                                delivery_date=str(order.get("expected_supply_date") or order.get("expected_delivery_date") or ""),
                                on_account=True,
                                # FIX: pass only live lines to post-save actions.
                                # Sending the raw all_lines included deleted rows,
                                # which made post-save reconcile against the wrong
                                # totals and skip the action panel.
                                lines=[_l for _l in all_lines if not bool(_l.get("is_deleted"))],
                            )
                            _post_save_actions_rendered = True
                        except Exception as _psa_err:
                            st.caption(f"Post-save actions unavailable: {_psa_err}")
                        with st.expander("⏸ Hold / 🚫 Cancel Order", expanded=False):
                            try:
                                from modules.backoffice.guidance import (
                                    render_hold_confirm_panel, render_cancel_confirm_panel,
                                )
                                _order_id_action  = str(order.get("id") or order.get("order_id") or "")
                                _order_no_action  = str(order.get("order_no") or "")
                                _cur_status_action = str(order.get("status") or "CONFIRMED").upper()
                                st.markdown("**⏸ Hold Order**")
                                render_hold_confirm_panel(_order_id_action, _order_no_action, _cur_status_action)
                                st.markdown("---")
                                st.markdown("**🚫 Cancel Order**")
                                render_cancel_confirm_panel(_order_id_action, _order_no_action, _cur_status_action)
                            except ImportError:
                                st.caption("guidance.py not installed")
                        st.balloons()
                    except Exception as _save_err:
                        try:
                            from modules.core.error_logger import log_error
                            log_error(_save_err, context="backoffice.save_to_order",
                                      payload={"order_no": order.get("order_no"),
                                               "order_type": order.get("order_type")})
                        except Exception:
                            pass
                        st.error(f"❌ Save failed: {_save_err}")

            _status_for_actions = str(order.get("status") or "").upper()

            # After a production rollback the order is CONFIRMED but the staff
            # want to RE-EDIT, not send WhatsApp. Collapse post-save actions into
            # an expander when the order was rolled back (no in-session save).
            # _post_save_actions_rendered=True means a save just happened — expand.
            # Otherwise (e.g., loaded fresh after rollback) wrap in a collapsed expander.
            _psa_just_saved = _post_save_actions_rendered  # True only if save ran this rerun
            _psa_oid = str(order.get("id") or order.get("order_id") or "")
            _psa_session_key = f"_bo_psa_just_saved_{_psa_oid}"
            if _psa_just_saved:
                st.session_state[_psa_session_key] = True
            _psa_expand = bool(st.session_state.get(_psa_session_key))

            if (
                not _post_save_actions_rendered
                and _status_for_actions in ("CONFIRMED", "IN_PRODUCTION", "READY_TO_BILL", "READY_FOR_BILLING")
            ):
                try:
                    from modules.post_save_actions import render_post_save_actions

                    def _do_psa():
                        render_post_save_actions(
                            order_no=str(order.get("order_no") or order.get("display_order_no") or ""),
                            party_name=str(order.get("party_name") or order.get("patient_name") or "Customer"),
                            mobile=str(order.get("mobile") or order.get("patient_mobile") or order.get("party_mobile") or ""),
                            total=float(total_billing or 0),
                            order_type=str(order.get("order_type") or "RETAIL"),
                            advance=float(order.get("advance_amount") or order.get("paid_amount") or 0),
                            delivery_date=str(order.get("expected_supply_date") or order.get("expected_delivery_date") or ""),
                            on_account=True,
                            lines=[_l for _l in all_lines if not bool(_l.get("is_deleted"))],
                        )

                    if _psa_expand:
                        # Just saved this session — show expanded (normal post-save flow)
                        _do_psa()
                    else:
                        # Loaded fresh after rollback (or any re-open without a save)
                        # Collapse so the edit form above is the first thing staff see
                        with st.expander("🚀 Post-Save Actions (WhatsApp / Notification)", expanded=False):
                            _do_psa()

                    with st.expander("⏸ Hold / 🚫 Cancel Order", expanded=False):
                        try:
                            from modules.backoffice.guidance import (
                                render_hold_confirm_panel, render_cancel_confirm_panel,
                            )
                            _order_id_action  = str(order.get("id") or order.get("order_id") or "")
                            _order_no_action  = str(order.get("order_no") or "")
                            _cur_status_action = str(order.get("status") or "CONFIRMED").upper()
                            st.markdown("**⏸ Hold Order**")
                            render_hold_confirm_panel(_order_id_action, _order_no_action, _cur_status_action)
                            st.markdown("---")
                            st.markdown("**🚫 Cancel Order**")
                            render_cancel_confirm_panel(_order_id_action, _order_no_action, _cur_status_action)
                        except ImportError:
                            st.caption("guidance.py not installed")
                except Exception as _psa_reopen_err:
                    st.caption(f"Confirmation actions unavailable: {_psa_reopen_err}")
    
        if False:
            # Documents tab — smart default based on what lines this order has
            has_inhouse = bool(order.get('inhouse_lines'))
            has_lab     = bool(order.get('lab_order_lines'))
            has_stock   = bool(order.get('stock_lines'))

            # Default to the most relevant tab for this order type
            if has_stock and not has_inhouse and not has_lab:
                default_doc = 'Labels'
            elif has_inhouse and not has_lab:
                default_doc = 'Job Cards'
            elif has_lab and not has_inhouse:
                default_doc = 'Lab Orders'
            else:
                default_doc = 'All'

            doc_options = ['Job Cards', 'Lab Orders', 'Labels', 'All']
            doc_type = st.radio(
                "Document Type",
                doc_options,
                index=doc_options.index(default_doc),
                horizontal=True,
                key=f"doc_type_{order_id}"
            )

            shown_any = False

            if doc_type in ['Job Cards', 'All']:
                if has_inhouse:
                    if not _jump_billing:
                        generate_job_cards(order)
                        shown_any = True
                    else:
                        st.info("📄 Job cards available — navigate here manually after billing.")
                        shown_any = True
                elif doc_type == 'Job Cards':
                    st.info("No in-house manufacturing lines on this order — no job cards to generate.")

            if doc_type in ['Lab Orders', 'All']:
                if has_lab:
                    generate_lab_orders(order)
                    shown_any = True
                elif doc_type == 'Lab Orders':
                    st.info("No external lab lines on this order — no lab orders to generate.")

            if doc_type in ['Labels', 'All']:
                if has_stock:
                    generate_labels(order)
                    shown_any = True
                elif doc_type == 'Labels':
                    st.info("No stock lines on this order — no labels to generate.")

            if doc_type == 'All' and not shown_any:
                st.warning("No documents to generate for this order yet. Add and allocate line items first.")
    
    if _bo_active_section == "📊 Status":
        with tab3:
            _t3_status = str(order.get("status") or "").upper()
            if _t3_status in ("HOLD", "CANCELLED"):
                try:
                    from modules.backoffice.guidance import render_hold_confirm_panel, render_stage_guidance
                    _t3_oid = str(order.get("id") or order.get("order_id") or "")
                    _t3_ono = str(order.get("order_no") or "")
                    if _t3_status == "HOLD":
                        render_hold_confirm_panel(_t3_oid, _t3_ono, "HOLD")
                    else:
                        render_stage_guidance("CANCELLED", compact=False)
                    st.markdown("---")
                except ImportError:
                    pass
            from modules.backoffice.order_status_window import render_order_status_window
            render_order_status_window(order)
    
    if _bo_active_section == "💰 Billing Summary":
        with tab4:
            # ── Billing Summary ───────────────────────────────────────────────
            st.markdown("### 💰 Billing Summary")

            # ── Pricing summary ───────────────────────────────────────────────
            locked_count    = sum(1 for line in all_lines if line.get("pricing_locked", False))
            _active_billing_lines = [_l for _l in all_lines if not bool(_l.get("is_deleted"))]
            _order_type_bill = str(order.get("order_type") or "WHOLESALE").upper()
            total_billing   = sum(_bo_line_amounts(line, _order_type_bill)["grand"] for line in _active_billing_lines)
            total_discount  = sum(float(line.get("discount_amount") or 0) for line in _active_billing_lines)
            total_allocated = sum(int(line.get("allocated_qty") or 0) for line in _active_billing_lines)

            if locked_count > 0:
                st.info(f"🔒 {locked_count} of {len(all_lines)} line(s) have locked pricing")

            _mc1, _mc2, _mc3, _mc4 = st.columns(4)
            _mc1.metric("Lines",          len(_active_billing_lines))
            _mc2.metric("Allocated",      total_allocated)
            _mc3.metric("Discount",       f"₹{total_discount:.2f}")
            _mc4.metric("Billing Total",  f"₹{total_billing:.2f}")

            # ── Line table with discount ──────────────────────────────────────
            def _prod_billing_label(line: dict) -> str:
                """Build full product label with index and coating for billing table."""
                import json as _bj_lbl
                _lp = line.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _bj_lbl.loads(_lp)
                    except Exception as _e:
                        logger.warning("Suppressed error: %s", _e)
                        _lp = {}
                _nm = str(
                    line.get("product_name")
                    or _lp.get("service_display_name")
                    or _lp.get("display_product_name")
                    or _lp.get("service_description")
                    or "N/A"
                )
                _coat = str(line.get("coating_type") or line.get("coating") or
                            _lp.get("coating_type") or _lp.get("coating") or "").strip()
                _idx  = str(line.get("index_value") or line.get("lens_index") or
                            _lp.get("index_value") or _lp.get("lens_index") or "").strip()
                label = _nm
                if _idx and _idx not in _nm:
                    label = f"{label} ({_idx})"
                if _coat and _coat not in _nm:
                    label = f"{label} {_coat}"
                return label


            billing_data = []
            _billing_stage_map = {}
            try:
                from modules.sql_adapter import run_query as _rq_bill_stage
                _stage_lids = [
                    str(_ln.get("line_id") or _ln.get("id") or "")
                    for _ln in _active_billing_lines
                    if _ln.get("line_id") or _ln.get("id")
                ]
                if _stage_lids:
                    _stage_rows = _rq_bill_stage(
                        "SELECT order_line_id::text AS lid, current_stage, is_closed "
                        "FROM job_master WHERE order_line_id = ANY(%(ids)s::uuid[])",
                        {"ids": _stage_lids},
                    ) or []
                    _billing_stage_map = {str(r.get("lid") or ""): r for r in _stage_rows}
            except Exception:
                _billing_stage_map = {}
            for _bi, line in enumerate(_active_billing_lines, 1):
                _bqty   = int(line.get("billing_qty") or 0)
                _bprice = float(line.get("unit_price") or 0)
                _bqty_label = _bo_qty_display(line)
                _bdisc  = float(line.get("discount_amount") or 0)
                _bamount = _bo_line_amounts(line, _order_type_bill)
                _btotal = _bamount["grand"]
                _bgross = _bqty * _bprice
                _line_stage_row = _billing_stage_map.get(str(line.get("line_id") or line.get("id") or "")) or {}
                _line_stage = str(_line_stage_row.get("current_stage") or "").upper()
                _line_lp_stage = line.get("lens_params") or {}
                if isinstance(_line_lp_stage, str):
                    try:
                        import json as _line_stage_json
                        _line_lp_stage = _line_stage_json.loads(_line_lp_stage)
                    except Exception:
                        _line_lp_stage = {}
                _line_route_stage = str(
                    line.get("manufacturing_route")
                    or _line_lp_stage.get("manufacturing_route")
                    or ""
                ).upper()
                _supplier_stage_label = str(
                    line.get("supplier_stage")
                    or _line_lp_stage.get("supplier_stage")
                    or _line_lp_stage.get("external_lab_stage")
                    or ""
                ).upper()
                _line_stage_label = (
                    "Ready to Bill" if bool(_line_stage_row.get("is_closed")) or _line_stage in ("READY_TO_BILL", "READY_FOR_BILLING")
                    else "Ready to Bill" if _line_route_stage in ("VENDOR", "EXTERNAL_LAB") and _supplier_stage_label in ("READY_TO_BILL", "READY_FOR_BILLING")
                    else _supplier_stage_label.replace("_", " ").title() if _line_route_stage in ("VENDOR", "EXTERNAL_LAB") and _supplier_stage_label
                    else (_line_stage.replace("_", " ").title() if _line_stage else "Pending")
                )
                billing_data.append({
                    "#":        _bi,
                    "Product":  _prod_billing_label(line),
                    "Eye":      str(line.get("eye_side", "")).upper(),
                    "Stage":    _line_stage_label,
                    "Qty":      _bqty_label,
                    "Unit ₹":   f"{_bprice:.2f}",
                    "Gross ₹":  f"{_bgross:.2f}",
                    "Disc ₹":   f"{_bdisc:.2f}" if _bdisc else "—",
                    "Total ₹":  f"{_btotal:.2f}",
                    "Route":    str(line.get("manufacturing_route") or "—").upper(),
                    "🔒":       "🔒" if line.get("pricing_locked") else "",
                })
            st.dataframe(pd.DataFrame(billing_data), use_container_width=True, hide_index=True)

            _present_service_families = {
                _bo_service_family_for_line(_svc_line)
                for _svc_line in _active_billing_lines
                if _bo_service_family_for_line(_svc_line)
            }
            _suspected_missing_services = _bo_suspected_missing_services(_active_billing_lines)
            _recovery_label = (
                f"⚠️ Possible Missing Services — {', '.join(_suspected_missing_services)}"
                if _suspected_missing_services
                else "🧾 Add missed service / collectable before billing"
            )
            with st.expander(_recovery_label, expanded=bool(_suspected_missing_services)):
                if _suspected_missing_services:
                    st.warning(
                        "The order data hints that these services may be missing from billing: "
                        + ", ".join(_suspected_missing_services)
                    )
                else:
                    st.caption("Use only when a service was taken/selected but is not visible in Billing Status.")
                try:
                    from modules.backoffice.service_master import fetch_service_types as _fst_rec, service_price as _sp_rec
                    _svc_recovery_rows = _bo_complete_service_rows(_fst_rec(active_only=True) or [])
                except Exception:
                    _svc_recovery_rows = _bo_complete_service_rows([])
                    _sp_rec = lambda s, ot, *args, **kwargs: float((s or {}).get("default_price") or 0)

                _svc_recovery_groups = ["COLOURING", "FITTING", "COURIER", "CONSULTATION", "EYE_TESTING", "MISC"]
                _default_group_idx = 0
                if _suspected_missing_services:
                    _default_group_idx = _svc_recovery_groups.index(_suspected_missing_services[0])
                _rec_key_base = str(order.get("id") or order_id or "")[:8]
                _rec_group = st.selectbox(
                    "Service family",
                    _svc_recovery_groups,
                    index=_default_group_idx,
                    format_func=lambda g: (
                        f"{g.title()} — already present" if g in _present_service_families else g.title()
                    ),
                    key=f"bo_bill_missing_svc_group_{_rec_key_base}",
                )
                _rec_options = [
                    r for r in _svc_recovery_rows
                    if str(r.get("service_group") or "").upper() == _rec_group
                ] or [{
                    "service_code": _rec_group,
                    "service_group": _rec_group,
                    "service_name": _rec_group.title(),
                    "gst_percent": 0 if _rec_group in ("CONSULTATION", "EYE_TESTING") else 18,
                    "default_price": 0,
                }]
                _rec_labels = [
                    f"{r.get('service_name') or r.get('service_code')} · {r.get('service_code') or _rec_group}"
                    for r in _rec_options
                ]
                _rec_i = st.selectbox(
                    "Service type",
                    range(len(_rec_options)),
                    format_func=lambda i: _rec_labels[i],
                    key=f"bo_bill_missing_svc_type_{_rec_key_base}_{_rec_group}",
                )
                _rec_def = _rec_options[int(_rec_i)]
                _rec_rate_default = float(_sp_rec(_rec_def, _order_type_bill) if _rec_def else 0)
                if _rec_rate_default <= 0:
                    _rec_rate_default = float(_rec_def.get("default_price") or 0)
                _rec_gst_default = float(
                    _rec_def.get("gst_percent")
                    if _rec_def.get("gst_percent") is not None
                    else (0 if _rec_group in ("CONSULTATION", "EYE_TESTING") else 18)
                )
                _rsvc1, _rsvc2, _rsvc3 = st.columns([1.2, 1.0, 1.0])
                with _rsvc1:
                    _rec_rate = st.number_input(
                        "₹ Rate / pair",
                        min_value=0.0,
                        value=float(_rec_rate_default or 0),
                        step=10.0,
                        key=f"bo_bill_missing_svc_rate_{_rec_key_base}_{_rec_group}",
                    )
                with _rsvc2:
                    _rec_qty_factor = st.selectbox(
                        "Qty",
                        [0.5, 1.0, 1.5, 2.0, 3.0],
                        index=1,
                        format_func=lambda v: (
                            f"{v:g} pair — 1 eye" if v == 0.5 else
                            f"{v:g} pair — both eyes" if v == 1.0 else
                            f"{v:g} pair"
                        ),
                        key=f"bo_bill_missing_svc_qty_{_rec_key_base}_{_rec_group}",
                    )
                with _rsvc3:
                    _rec_gst = st.number_input(
                        "GST %",
                        min_value=0.0,
                        max_value=28.0,
                        value=float(_rec_gst_default),
                        step=0.5,
                        key=f"bo_bill_missing_svc_gst_{_rec_key_base}_{_rec_group}",
                    )
                _rec_instr = ""
                _rec_photo = None
                if _rec_group in ("COLOURING", "FITTING"):
                    _rec_instr = st.text_area(
                        "Instruction for production / provider",
                        placeholder="Tint shade, sample reference, fitting note, urgency...",
                        key=f"bo_bill_missing_svc_instr_{_rec_key_base}_{_rec_group}",
                        height=70,
                    )
                    if _rec_group == "COLOURING":
                        _rec_photo = st.file_uploader(
                            "Colour sample photograph",
                            type=["jpg", "jpeg", "png", "webp"],
                            key=f"bo_bill_missing_svc_photo_{_rec_key_base}_{_rec_group}",
                        )
                elif _rec_group in ("COURIER", "CONSULTATION", "EYE_TESTING", "MISC"):
                    _rec_instr = st.text_input(
                        "Narration / reference",
                        placeholder="Optional note",
                        key=f"bo_bill_missing_svc_note_{_rec_key_base}_{_rec_group}",
                    )
                _rec_duplicate = _rec_group in _present_service_families
                if _rec_duplicate:
                    st.info(f"{_rec_group.title()} already exists on this order. Add only if this is a genuine extra charge.")
                if st.button(
                    f"✅ Add {_rec_group.title()} Service Line",
                    key=f"bo_bill_missing_svc_add_{_rec_key_base}_{_rec_group}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        import base64 as _rec_b64
                        import json as _rec_json
                        import uuid as _rec_uuid
                        from modules.sql_adapter import run_write as _rw_rec
                        _oid_rec = str(order.get("id") or order.get("order_id") or order_id or "")
                        if not _oid_rec:
                            st.error("Order id missing. Reopen this order and try again.")
                            st.stop()
                        if _rec_duplicate and not st.session_state.get(f"bo_bill_missing_svc_dup_{_rec_key_base}_{_rec_group}"):
                            st.warning(f"{_rec_group.title()} already exists. Click Add again to confirm duplicate.")
                            st.session_state[f"bo_bill_missing_svc_dup_{_rec_key_base}_{_rec_group}"] = True
                            st.stop()
                        _direct_rec = _rec_group not in ("COLOURING", "FITTING")
                        _rec_base = round(float(_rec_rate or 0) * float(_rec_qty_factor or 1), 2)
                        _rec_total, _rec_gst_amt = _bo_service_line_amounts(_rec_base, _rec_gst, order.get("order_type"))
                        _rec_name = str(_rec_def.get("service_name") or _rec_group.title())
                        _rec_code = str(_rec_def.get("service_code") or _rec_group).upper()
                        _rec_label = f"{_rec_group.title()}: {_rec_name}"
                        _rec_pid = _ensure_bo_service_product(_rec_group, _rec_label, _rec_gst)
                        _rec_photo_b64 = ""
                        _rec_photo_name = ""
                        if _rec_photo:
                            _rec_photo_b64 = _rec_b64.b64encode(_rec_photo.read()).decode("ascii")
                            _rec_photo_name = _rec_photo.name
                        _lp_rec = _rec_json.dumps({
                            "charge_type": _rec_group,
                            "service_type": _rec_group,
                            "service_group": _rec_group,
                            "service_code": _rec_code,
                            "service_description": _rec_name,
                            "service_display_name": _rec_label,
                            "display_product_name": _rec_label,
                            "service_production_type": "" if _direct_rec else _rec_group,
                            "manufacturing_route": _service_route_for_group(_rec_group, _direct_rec),
                            "service_instruction": _rec_instr,
                            "colour_sample_photo": _rec_photo_b64,
                            "colour_sample_filename": _rec_photo_name,
                            "service_qty_factor": float(_rec_qty_factor or 1),
                            "service_rate_per_pair": float(_rec_rate or 0),
                            "service_origin": "billing_summary_recovery",
                        })
                        _rw_rec("""
                            INSERT INTO order_lines
                              (id, order_id, product_id, eye_side,
                               unit_price, total_price, billing_total,
                               gst_percent, gst_amount,
                               quantity, billing_qty, allocated_qty,
                               is_service_line, batch_status, lens_params)
                            VALUES
                              (%(id)s::uuid, %(oid)s::uuid, %(pid)s::uuid, 'S',
                               %(up)s, %(tp)s, %(tp)s,
                               %(gp)s, %(ga)s,
                               1, 1, %(aq)s,
                               TRUE, %(bs)s, %(lp)s::jsonb)
                        """, {
                            "id": str(_rec_uuid.uuid4()),
                            "oid": _oid_rec,
                            "pid": _rec_pid,
                            "up": _rec_base,
                            "tp": _rec_total,
                            "gp": float(_rec_gst or 0),
                            "ga": _rec_gst_amt,
                            "aq": 1 if _direct_rec else 0,
                            "bs": "READY" if _direct_rec else "PENDING",
                            "lp": _lp_rec,
                        })
                        try:
                            from modules.backoffice.backoffice_helpers import ensure_order_production_refs
                            ensure_order_production_refs(order_id=_oid_rec)
                        except Exception as _rec_ref_err:
                            logger.warning("Service recovery production_ref fill failed: %s", _rec_ref_err)
                        _bo_refresh_order_total_value(_oid_rec)
                        st.session_state.pop(f"bo_bill_missing_svc_dup_{_rec_key_base}_{_rec_group}", None)
                        if _direct_rec:
                            st.success(f"{_rec_group.title()} service line added and ready for billing.")
                        else:
                            st.success(f"{_rec_group.title()} service line added. Complete it in Production before billing.")
                        st.rerun()
                    except Exception as _rec_add_err:
                        st.error(f"Could not add service line: {_rec_add_err}")

            # ── Mixed-route audit: one order may have many independent line routes ──
            _route_notes = []
            _route_errors = []
            _route_stage_map = {}
            try:
                from modules.sql_adapter import run_query as _rq_route_stage
                _route_lids = [
                    str(_ln.get("line_id") or _ln.get("id") or "")
                    for _ln in all_lines
                    if _ln.get("line_id") or _ln.get("id")
                ]
                if _route_lids:
                    _stage_rows = _rq_route_stage(
                        "SELECT order_line_id::text AS lid, current_stage, is_closed "
                        "FROM job_master WHERE order_line_id = ANY(%(ids)s::uuid[])",
                        {"ids": _route_lids},
                    ) or []
                    _route_stage_map = {str(r.get("lid") or ""): r for r in _stage_rows}
            except Exception:
                _route_stage_map = {}
            for _ln in all_lines:
                _lp = _ln.get("lens_params") or {}
                if isinstance(_lp, str):
                    try:
                        import json as _rj_audit
                        _lp = _rj_audit.loads(_lp)
                    except Exception:
                        _lp = {}
                _name = str(_ln.get("product_name") or _lp.get("service_display_name") or _lp.get("service_description") or "Line")
                _route = str(_ln.get("manufacturing_route") or _lp.get("manufacturing_route") or "").upper()
                _is_svc = bool(_ln.get("is_service_line")) or str(_ln.get("eye_side") or "").upper() in ("S", "SERVICE")
                _svc_type = str(_lp.get("service_production_type") or "").upper()
                _lid_audit = str(_ln.get("line_id") or _ln.get("id") or "")
                _stage_row = _route_stage_map.get(_lid_audit) or {}
                _stage_audit = str(_stage_row.get("current_stage") or "").upper()
                _stage_label_audit = (
                    "Ready to Bill" if bool(_stage_row.get("is_closed")) or _stage_audit in ("READY_TO_BILL", "READY_FOR_BILLING")
                    else (_stage_audit.replace("_", " ").title() if _stage_audit else "Pending / job not created")
                )
                if _is_svc:
                    if not _svc_type:
                        _route_notes.append(f"{_name}: direct billing service.")
                    elif _svc_type == "COLOURING":
                        _route_notes.append(f"{_name}: COLOURING service — {_stage_label_audit}.")
                    elif _svc_type == "FITTING":
                        _route_notes.append(f"{_name}: FITTING service — {_stage_label_audit}.")
                    else:
                        _route_notes.append(f"{_name}: {_svc_type} service route {_route}.")
                else:
                    _route_notes.append(f"{_name}: product route {_route or 'NOT ASSIGNED'}.")
                    if not _route:
                        _route_errors.append(f"{_name}: product route not assigned.")

            with st.expander("🧭 Mixed Order Route Audit", expanded=bool(_route_errors)):
                if _route_errors:
                    for _err in _route_errors:
                        st.error(_err)
                    st.caption("Fix these before challan/invoice. Every line must have its own clear route.")
                else:
                    st.success("Line-wise routing is consistent. Mixed orders are allowed.")
                for _note in _route_notes:
                    st.caption(f"• {_note}")

            st.markdown("---")

            # ── INLINE BILL NOW ───────────────────────────────────────────────
            # Check which lines are ready to bill (not yet on any challan)
            _bill_ready = []
            _bill_blocked = ""
            _bill_blockers = []
            _bill_pending_rows = []
            try:
                from modules.sql_adapter import run_query as _rq_b4
                _BILL_READY_STAGES = {
                    # Strict billing gate:
                    # READY_FOR_PACK is not billable. Packing must advance to READY_TO_BILL first.
                    "READY_TO_BILL", "READY_FOR_BILLING",
                }

                # WIN 3: pre-fetch all job_master rows for this order's lines in ONE query
                # instead of one per line inside the loop (was 2×N queries → now 1 query).
                _jm_line_ids_b4 = [
                    str(_bl.get("line_id") or _bl.get("id") or "")
                    for _bl in _active_billing_lines
                    if _bl.get("line_id") or _bl.get("id")
                ]
                _jm_map_b4 = {}
                if _jm_line_ids_b4:
                    try:
                        _jm_rows_b4 = _rq_b4(
                            "SELECT order_line_id::text AS lid, current_stage, is_closed "
                            "FROM job_master WHERE order_line_id = ANY(%(ids)s::uuid[])",
                            {"ids": _jm_line_ids_b4},
                        ) or []
                        _jm_map_b4 = {str(r["lid"]): r for r in _jm_rows_b4}
                    except Exception:
                        _jm_map_b4 = {}

                _already_billed_line_ids_b4 = set()
                if _jm_line_ids_b4:
                    try:
                        _already_rows_b4 = _rq_b4("""
                            SELECT DISTINCT cl.order_line_id::text AS lid
                            FROM challan_lines cl
                            JOIN challans c ON c.id = cl.challan_id
                            WHERE cl.order_line_id = ANY(%(ids)s::uuid[])
                              AND c.status NOT IN ('CANCELLED','VOID')
                              AND NOT COALESCE(cl.is_deleted,FALSE)
                              AND NOT COALESCE(c.is_deleted,FALSE)
                        """, {"ids": _jm_line_ids_b4}) or []
                        _already_billed_line_ids_b4 = {
                            str(r.get("lid") or "") for r in _already_rows_b4 if r.get("lid")
                        }
                    except Exception:
                        _already_billed_line_ids_b4 = set()

                for _bl in _active_billing_lines:
                    _bl_id    = str(_bl.get("line_id") or _bl.get("id") or "")
                    _lp_route_src = _bl.get("lens_params") or {}
                    if isinstance(_lp_route_src, str):
                        try:
                            import json as _jrte
                            _lp_route_src = _jrte.loads(_lp_route_src)
                        except Exception:
                            _lp_route_src = {}
                    _bl_route = str(_bl.get("manufacturing_route") or _lp_route_src.get("manufacturing_route") or "").upper()
                    _bl_price = _bo_line_amounts(_bl, _order_type_bill)["grand"]
                    if _bl_price <= 0:
                        continue  # skip zero-value lines

                    # Already billed? Pre-fetched once for all visible lines above.
                    if _bl_id in _already_billed_line_ids_b4:
                        continue

                    def _add_pending_bill_row(_line, _reason: str) -> None:
                        _bill_pending_rows.append({
                            "Product": _prod_billing_label(_line),
                            "Eye": str(_line.get("eye_side") or "").upper(),
                            "Stage / Reason": _reason,
                            "Amount ₹": f"{_bo_line_amounts(_line, _order_type_bill)['grand']:.2f}",
                        })

                    # Service lines are ALWAYS ready to bill — no job card or blank needed
                    _bl_is_svc = (
                        bool(_bl.get("is_service_line"))
                        or str(_bl.get("eye_side","")).upper() in ("S","SERVICE")
                        or str(_bl.get("manufacturing_route","")).upper() == "SERVICE"
                        or str(_lp_route_src.get("manufacturing_route","")).upper() == "SERVICE"
                        or str(_lp_route_src.get("service_production_type","")).upper() in ("COLOURING", "FITTING")
                    )
                    if _bl_is_svc or _bl_route != "INHOUSE":
                        # Service lines and non-inhouse lines: always billable once stages done
                        # (Service lines completed colouring/fitting stages = ready)
                        if _bl_is_svc:
                            # Check service has reached a terminal production stage
                            # (or is a direct-billing service like COURIER)
                            _lp_bl = _bl.get("lens_params") or {}
                            if isinstance(_lp_bl, str):
                                try:
                                    import json as _jbl; _lp_bl = _jbl.loads(_lp_bl)
                                except Exception as _e:
                                    logger.warning("Suppressed error: %s", _e)
                                    _lp_bl = {}
                            _svc_prod_type = str(_lp_bl.get("service_production_type") or "").upper()
                            _jm_svc = [_jm_map_b4[_bl_id]] if _bl_id in _jm_map_b4 else (
                                _rq_b4("""
                                SELECT current_stage, is_closed FROM job_master
                                WHERE order_line_id = %(lid)s::uuid LIMIT 1
                            """, {"lid": _bl_id}) if _bl_id else []
                            )
                            if not _svc_prod_type or _svc_prod_type not in ("COLOURING","FITTING"):
                                # Direct billing service (COURIER etc) — always ready
                                _bill_ready.append(_bl)
                            elif _svc_prod_type in ("COLOURING", "FITTING") and _jm_svc:
                                _svc_stg  = str(_jm_svc[0].get("current_stage") or "").upper()
                                _svc_clsd = bool(_jm_svc[0].get("is_closed"))
                                if _svc_clsd or _svc_stg in _BILL_READY_STAGES:
                                    _bill_ready.append(_bl)
                                else:
                                    _add_pending_bill_row(
                                        _bl,
                                        f"{_svc_prod_type.title()} stage {_svc_stg or 'NOT STARTED'}",
                                    )
                                    _bill_blockers.append(
                                        f"{str(_bl.get('product_name',''))[:20]}: "
                                        f"stage {_svc_stg or 'NOT STARTED'}"
                                    )
                            else:
                                _add_pending_bill_row(
                                    _bl,
                                    f"{_svc_prod_type.title() if _svc_prod_type else 'Service'} job not created",
                                )
                                _bill_blockers.append(
                                    f"{str(_bl.get('product_name',''))[:20]}: "
                                    f"{_svc_prod_type.lower() if _svc_prod_type else 'service'} job not created"
                                )
                        else:
                            if _bl_route in ("VENDOR", "EXTERNAL_LAB"):
                                _lp_vnd = _bl.get("lens_params") or {}
                                if isinstance(_lp_vnd, str):
                                    try:
                                        import json as _jvnd
                                        _lp_vnd = _jvnd.loads(_lp_vnd)
                                    except Exception:
                                        _lp_vnd = {}
                                _sup_stage = str(
                                    _bl.get("supplier_stage")
                                    or _lp_vnd.get("supplier_stage")
                                    or _lp_vnd.get("external_lab_stage")
                                    or ""
                                ).upper()
                                if _sup_stage in _BILL_READY_STAGES:
                                    _bill_ready.append(_bl)
                                else:
                                    _add_pending_bill_row(
                                        _bl,
                                        f"{_bl_route} stage {_sup_stage or 'ORDER_PLACED'}",
                                    )
                                    _bill_blockers.append(
                                        f"{str(_bl.get('product_name',''))[:20]} "
                                        f"[{_bl_route}]: supplier stage {_sup_stage or 'ORDER_PLACED'}"
                                    )
                            else:
                                _bill_ready.append(_bl)
                    else:
                        # Inhouse lens: check job stage
                        _jm = [_jm_map_b4[_bl_id]] if _bl_id in _jm_map_b4 else (
                            _rq_b4("""
                            SELECT current_stage, is_closed FROM job_master
                            WHERE order_line_id = %(lid)s::uuid LIMIT 1
                        """, {"lid": _bl_id}) if _bl_id else []
                        )
                        if _jm:
                            _stg   = str(_jm[0].get("current_stage") or "").upper()
                            _clsd  = bool(_jm[0].get("is_closed"))
                            if _clsd or _stg in _BILL_READY_STAGES:
                                _bill_ready.append(_bl)
                            else:
                                _add_pending_bill_row(
                                    _bl,
                                    f"{_bl_route or 'INHOUSE'} stage {_stg or 'NOT STARTED'}",
                                )
                                _bill_blockers.append(
                                    f"{str(_bl.get('product_name',''))[:20]} "
                                    f"[{_bl_route}]: stage {_stg or 'NOT STARTED'}"
                                )
                        else:
                            # No job card yet — not ready
                            _add_pending_bill_row(_bl, "Job card not created yet")
                            _bill_blockers.append(
                                f"{str(_bl.get('product_name',''))[:20]}: "
                                "Job card not created yet"
                            )
                # Partial billing rule:
                # If some lines are ready, allow challan for those ready lines and keep
                # unfinished services/products pending for a later challan. A hard block
                # is only needed when nothing is ready to bill.
                if _bill_blockers and not _bill_ready:
                    _bill_blocked = " | ".join(dict.fromkeys(_bill_blockers))
            except Exception as _b4e:
                st.caption(f"Billing readiness check: {_b4e}")

            if _bill_pending_rows:
                with st.expander(f"⏳ Pending Billing Lines — {len(_bill_pending_rows)}", expanded=True):
                    st.dataframe(pd.DataFrame(_bill_pending_rows), use_container_width=True, hide_index=True)

            _bill_total = sum(_bo_line_amounts(l, _order_type_bill)["grand"] for l in _bill_ready)
            _bill_lbl = (
                f"💰 Bill Now — {len(_bill_ready)} line(s) · ₹{_bill_total:,.2f}"
                if _bill_ready else "💰 Billing (not ready)"
            )
            try:
                _existing_doc_rows = _rq_b4(
                    """
                    SELECT 1
                    FROM challans c
                    WHERE (c.order_ids::text[] @> ARRAY[%(oid)s::text]
                        OR c.order_ids::text[] @> ARRAY[%(ono)s::text])
                      AND COALESCE(c.status,'') NOT IN ('VOID','CANCELLED','DELETED')
                    LIMIT 1
                    """,
                    {"oid": str(order.get("id") or order_id), "ono": str(order.get("order_no") or "")},
                ) or []
                _has_existing_billing_docs = bool(_existing_doc_rows)
            except Exception:
                _has_existing_billing_docs = False

            with st.expander(
                _bill_lbl,
                expanded=(
                    (bool(_bill_ready) and not _bill_blocked and _jump_billing)
                    or _has_existing_billing_docs
                )
            ):
                if _bill_blocked:
                    st.warning(
                        "Cannot bill — route/stage not ready: "
                        + _bill_blocked
                        + ". Advance the relevant supplier/production flow to Ready to Bill first."
                    )
                elif _bill_blockers and _bill_ready:
                    st.info(
                        "Partial billing enabled — only ready lines below will be billed now. "
                        "Pending lines remain open for later billing after their stage is complete."
                    )
                if not _bill_ready:
                    if not _bill_blocked:
                        st.info("No unbilled lines ready on this order.")
                else:
                    # Show what will be billed
                    for _rl in _bill_ready:
                        _rl_eye   = str(_rl.get("eye_side","")).upper()
                        _rl_name  = str(_rl.get("product_name","")).split(" | ")[0][:35]
                        _rl_qty   = _bo_qty_display(_rl)
                        _rl_disc  = float(_rl.get("discount_amount") or 0)
                        _rl_total = _bo_line_amounts(_rl, _order_type_bill)["grand"]
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;"
                            f"padding:3px 0;border-bottom:1px solid #1e293b;font-size:0.8rem'>"
                            f"<span>{_rl_eye} {_rl_name} <span style='color:#94a3b8'>· {_rl_qty}</span></span>"
                            f"<span style='color:#10b981;font-weight:700'>₹{_rl_total:,.2f}"
                            + (f" <span style='color:#64748b;font-size:0.7rem'>(-₹{_rl_disc:.2f})</span>"
                               if _rl_disc > 0 else "")
                            + "</span></div>",
                            unsafe_allow_html=True,
                        )

                    st.markdown(
                        f"<div style='text-align:right;color:#10b981;font-weight:800;"
                        f"font-size:1rem;padding:8px 0'>Total: ₹{_bill_total:,.2f}</div>",
                        unsafe_allow_html=True,
                    )

                    _xc1, _xc2 = st.columns(2)
                    _chal_no  = _xc1.text_input("Challan No",
                                                  key=f"bo_chal_{order_id}",
                                                  placeholder="Auto-generated if blank")
                    _remarks  = _xc2.text_input("Remarks",
                                                  key=f"bo_rem_{order_id}",
                                                  placeholder="Optional")
                    _preflight_ok = True
                    _preflight_issues = []
                    _preflight_line_ids = [
                        str(l.get("line_id") or l.get("id", ""))
                        for l in _bill_ready
                        if str(l.get("line_id") or l.get("id", "")).strip()
                    ]
                    try:
                        from modules.billing.challan_invoice_manager import audit_billing_preflight
                        _preflight_ok, _preflight_issues = audit_billing_preflight(
                            [str(order.get("id") or order_id)],
                            _preflight_line_ids,
                        )
                    except Exception as _preflight_err:
                        _preflight_ok = False
                        _preflight_issues = [f"Billing audit could not run: {_preflight_err}"]
                    if not _preflight_ok:
                        st.warning(
                            "Billing audit warning: possible missing/unusual collectables were found. "
                            "This partial challan will include only the ready lines listed above."
                        )
                        for _pfi in _preflight_issues:
                            st.write("•", _pfi)
                        if _bill_ready:
                            _preflight_ok = True

                    _do_chal = st.button(
                        f"🧾 Create Challan — ₹{_bill_total:,.2f}",
                        key=f"bo_do_chal_{order_id}",
                        type="primary", use_container_width=True,
                        disabled=not _preflight_ok,
                    )
                    _do_inv = st.button(
                        "🧾 → 📄 Challan + Invoice",
                        key=f"bo_do_inv_{order_id}",
                        use_container_width=True,
                        disabled=not _preflight_ok,
                    )

                    if _do_chal or _do_inv:
                        try:
                            from modules.billing.challan_invoice_manager import create_challan
                            from modules.sql_adapter import run_query as _rq_ch

                            # ── Repair missing blank_allocations ─────────────────────────
                            # Job card saves blank selection into lens_params.surfacing_data
                            # but may not always write a row to blank_allocations (older
                            # pipeline versions, network blip during save, etc.).
                            # Billing readiness checks blank_allocations — if the row is
                            # absent the challan gate says "no blank allocated" even though
                            # the technician did allot the blank.
                            # This runs silently before challan creation; it is idempotent
                            # (ON CONFLICT DO UPDATE) so safe to call every time.
                            def _repair_missing_blank_allocations(line_ids: list) -> None:
                                import json as _rj
                                from modules.sql_adapter import run_query as _rq_rep, run_write as _rw_rep
                                for _lid in line_ids:
                                    if not _lid:
                                        continue
                                    try:
                                        _lrows = _rq_rep(
                                            "SELECT lens_params, eye_side FROM order_lines "
                                            "WHERE id = %(lid)s::uuid LIMIT 1",
                                            {"lid": _lid},
                                        )
                                        if not _lrows:
                                            continue
                                        _lp = _lrows[0].get("lens_params") or {}
                                        if isinstance(_lp, str):
                                            try:
                                                _lp = _rj.loads(_lp)
                                            except Exception:
                                                _lp = {}
                                        _surf     = (_lp.get("surfacing_data") or {}) if isinstance(_lp, dict) else {}
                                        _blank_id = _surf.get("blank_id") or _surf.get("selected_blank_id")
                                        if not _blank_id:
                                            continue
                                        # Check if allocation row already exists
                                        _existing = _rq_rep(
                                            "SELECT 1 FROM blank_allocations "
                                            "WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                                            {"lid": _lid},
                                        )
                                        if _existing:
                                            continue  # already allocated — nothing to repair
                                        # Write the missing row
                                        _rw_rep("""
                                            INSERT INTO blank_allocations
                                                (id, order_line_id, blank_id, eye_side,
                                                 base_selected, allocated_at)
                                            VALUES (
                                                gen_random_uuid(),
                                                %(lid)s::uuid,
                                                %(bid)s::uuid,
                                                %(eye)s,
                                                %(base)s,
                                                NOW()
                                            )
                                            ON CONFLICT (order_line_id) DO UPDATE SET
                                                blank_id      = EXCLUDED.blank_id,
                                                eye_side      = EXCLUDED.eye_side,
                                                base_selected = EXCLUDED.base_selected,
                                                allocated_at  = NOW()
                                        """, {
                                            "lid":  _lid,
                                            "bid":  str(_blank_id),
                                            "eye":  _lrows[0].get("eye_side") or "",
                                            "base": _surf.get("base_curve") or _surf.get("base_selected"),
                                        })
                                    except Exception:
                                        pass  # non-fatal — challan creation will surface any hard block

                            _repair_line_ids = [
                                str(l.get("line_id") or l.get("id", ""))
                                for l in _bill_ready
                            ]
                            _repair_missing_blank_allocations(_repair_line_ids)

                            # Compute base amount + tax from per-line GST
                            _t_base = 0.0
                            _t_tax  = 0.0
                            for _l in _bill_ready:
                                _amt = _bo_line_amounts(_l, _order_type_bill)
                                _t_base += _amt["taxable"]
                                _t_tax  += _amt["gst"]

                            _challan_no_out = create_challan(
                                party_id     = str(order.get("party_id") or ""),
                                order_ids    = [str(order.get("id") or order_id)],
                                total_amount = round(_t_base, 2),
                                total_tax    = round(_t_tax,  2),
                                remarks      = _remarks.strip() or "",
                                line_ids     = [str(l.get("line_id") or l.get("id", ""))
                                                for l in _bill_ready],
                            )
                            if _challan_no_out:
                                st.success(
                                    f"✅ Challan {_challan_no_out} created · "
                                    f"₹{_bill_total:,.2f}"
                                )
                                # Direct navigation button to Challan Dashboard
                                if st.button("📋 View in Challan Dashboard →",
                                             key=f"go_chal_dash_{order_id}",
                                             use_container_width=True):
                                    st.session_state["_sidebar_page"]  = "🧾  Challan & Invoice"
                                    st.session_state["active_module"]  = "Challan & Invoice Dashboard"
                                    st.session_state["bo_show_billing_tab"] = False
                                    st.rerun()
                                if _do_inv:
                                    try:
                                        from modules.billing.challan_invoice_manager import (
                                            create_invoice,
                                        )
                                        # Fetch the newly created challan's UUID
                                        _ch_rows = _rq_ch(
                                            "SELECT id::text AS challan_id FROM challans "
                                            "WHERE challan_no = %(n)s LIMIT 1",
                                            {"n": _challan_no_out},
                                        )
                                        _ch_id = (_ch_rows[0]["challan_id"]
                                                  if _ch_rows else None)
                                        if _ch_id:
                                            _inv_no = create_invoice(
                                                challan_id   = _ch_id,
                                                party_id     = str(order.get("party_id") or ""),
                                                order_ids    = [str(order.get("id") or order_id)],
                                                total_amount = round(_t_base, 2),
                                                total_tax    = round(_t_tax,  2),
                                                remarks      = _remarks.strip() or "",
                                            )
                                            if _inv_no:
                                                st.success(f"📄 Invoice {_inv_no} created")
                                            else:
                                                st.warning("Challan created — invoice creation failed, retry from Challan Dashboard")
                                        else:
                                            st.warning("Challan created — could not auto-create invoice (challan not found in DB)")
                                    except Exception as _ie:
                                        st.warning(f"Invoice error: {_ie}")
                                import time; time.sleep(0.4)
                                # Keep billing tab active on next render
                                st.session_state["bo_show_billing_tab"] = True
                                st.rerun()
                            else:
                                st.error("Challan creation failed — check logs")
                        except Exception as _ce:
                            _err_msg = str(_ce)
                            st.error(f"❌ Billing error: {_err_msg}")
                            # Show detailed reason if it's a readiness block
                            if "not ready" in _err_msg.lower() or "stage:" in _err_msg.lower():
                                st.info(
                                    "💡 To fix: go to Production → In-house Lab → "
                                    "advance the job to **Ready to Bill**, then return here."
                                )
                            elif "already has an active challan" in _err_msg:
                                st.info("💡 Refresh the page — a challan may already exist for this order.")

            # ── Existing Challans + Payment Collection ────────────────────────
            # billing_status_ui renders: challan list, payment status, 
            # Convert to Invoice button, payment balance check
            st.markdown("---")
            try:
                from modules.backoffice.billing_status_ui import render_billing_status_panel
                render_billing_status_panel(order, all_lines, actions_enabled=False)
            except ImportError:
                st.info(
                    "💡 Challan management available in the **💳 Billing Gate** tab. "
                    "Use that tab to view existing challans, collect payment, and convert to invoice."
                )
            except Exception as _bse:
                st.warning(f"Billing status panel error: {_bse}")

            # ── Pricing debug toggle ──────────────────────────────────────────
            st.markdown("---")
            if st.checkbox("🔍 Debug Pricing (Advanced)", key=f"debug_pricing_{order_id}"):
                st.markdown("#### Line Item Debug")
                for _di, line in enumerate(all_lines, 1):
                    with st.expander(f"Line {_di}: {line.get('product_name', 'N/A')}", expanded=False):
                        st.json({
                            "product_id":           line.get("product_id", "N/A"),
                            "price_source":         line.get("price_source", "unknown"),
                            "eye_side":             line.get("eye_side", "N/A"),
                            "billing_qty":          line.get("billing_qty", 0),
                            "allocated_qty":        line.get("allocated_qty", 0),
                            "pending_qty":          max(0, int(line.get("billing_qty") or 0) -
                                                         int(line.get("allocated_qty") or 0)),
                            "unit_price":           line.get("unit_price", 0),
                            "discount_amount":      line.get("discount_amount", 0),
                            "discount_percent":     line.get("discount_percent", 0),
                            "billing_total":        line.get("billing_total", 0),
                            "manufacturing_route":  line.get("manufacturing_route", "N/A"),
                            "batch_allocation":     line.get("batch_allocation", []),
                            "pricing_locked":       line.get("pricing_locked", False),
                        }, expanded=False)
    
    # =====================================================
    # TAB 5: SUPPLIER ORDERS PANEL
    # =====================================================
    if False:
        _supplier_panel_key = f"bo_load_supplier_panel_{order_id}"
        if not st.session_state.get(_supplier_panel_key):
            st.info("Supplier Orders loads on demand to keep Backoffice fast.")
            if st.button("Load Supplier Orders", key=f"{_supplier_panel_key}_btn", use_container_width=True):
                st.session_state[_supplier_panel_key] = True
                st.rerun()
        else:
            if st.button("Hide Supplier Orders", key=f"{_supplier_panel_key}_hide", use_container_width=True):
                st.session_state.pop(_supplier_panel_key, None)
                st.rerun()
            try:
                from .supplier_panel import render_supplier_panel
                render_supplier_panel(order)
            except ImportError as e:
                st.error(f"❌ Supplier Panel module not found: {e}")
                st.info("📋 Place supplier_panel.py in modules/backoffice/ directory")
            except Exception as e:
                st.error(f"❌ Supplier Panel error: {e}")
                import traceback
                with st.expander("Debug Info"):
                    st.code(traceback.format_exc())
    
    # =====================================================
    # TAB 6: BILLING GATE (CONTROLLED WRITE PANEL)
    # =====================================================
    if False:
        _billing_gate_key = f"bo_load_billing_gate_{order_id}"
        if not st.session_state.get(_billing_gate_key):
            st.info("Billing Gate loads on demand. Billing Summary remains available immediately.")
            if st.button("Load Billing Gate", key=f"{_billing_gate_key}_btn", use_container_width=True):
                st.session_state[_billing_gate_key] = True
                st.rerun()
        else:
            if st.button("Hide Billing Gate", key=f"{_billing_gate_key}_hide", use_container_width=True):
                st.session_state.pop(_billing_gate_key, None)
                st.rerun()
            try:
                from .billing_gate import render_billing_gate
                render_billing_gate(order)
            except ImportError as e:
                st.error(f"❌ Billing Gate module not found: {e}")
                st.info("📋 Place billing_gate.py in modules/backoffice/ directory")

            except Exception as e:
                st.error(f"❌ Billing Gate error: {e}")
                import traceback
                with st.expander("Debug Info"):
                    st.code(traceback.format_exc())




    # ── TAB 7: DISPATCH — the final step after billing ────────────────────
    if _bo_active_section == "🚀 Dispatch":
        with tab7:
            _dispatch_status = str(order.get("status","")).upper()
            _billed_statuses = {
                "BILLED","CHALLANED","INVOICED","INVOICED_BILLED",
                "READY_TO_DISPATCH","CHALLAN_ONLY",
                "DISPATCHED","DELIVERED","CLOSED",
            }
            if _dispatch_status not in _billed_statuses:
                st.info(
                    "🔒 **Dispatch is available only after billing is complete.**\n\n"
                    "Create a Challan or Invoice first, then return here to dispatch."
                )
                st.markdown(
                    "<div style='background:#0f172a;border:1px solid #1e3a5f;"
                    "border-radius:8px;padding:16px 20px;margin:10px 0'>"
                    "<div style='color:#e2e8f0;font-size:0.82rem;line-height:2.2'>"
                    "1️⃣ &nbsp;Confirm Order &amp; Produce<br/>"
                    "2️⃣ &nbsp;Create Challan / Invoice<br/>"
                    "3️⃣ &nbsp;<b style='color:#6366f1'>🚀 Dispatch</b> ← next step<br/>"
                    "4️⃣ &nbsp;Confirm Delivery"
                    "</div></div>",
                    unsafe_allow_html=True,
                )
            else:
                try:
                    from modules.backoffice.dispatch_panel import render_dispatch_panel
                    render_dispatch_panel(order)
                except Exception as _dp_err:
                    st.error(f"Dispatch panel error: {_dp_err}")
def show_status_update_modal(order: Dict):
    """Show modal for updating order status"""
    st.markdown("---")
    st.markdown("###  Update Order Status")
    
    current_status = order.get('status', 'PENDING')

    
    new_status = st.selectbox(
        "New Status",
        [s.value for s in OrderStatus],
        index=[s.value for s in OrderStatus].index(current_status) if current_status in [s.value for s in OrderStatus] else 0,
        key=f"status_update_{get_display_order_id(order)}"
    )
    
    notes = st.text_area(
        "Status Update Notes",
        key=f"status_notes_{get_display_order_id(order)}"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button(" Update Status", type="primary", use_container_width=True):
            # Update order status
            for o in st.session_state.bo_active_orders:
                if get_display_order_id(o) == get_display_order_id(order):
                    o['status'] = new_status
                    o['updated_at'] = datetime.datetime.now().isoformat()
                    if notes:
                        if 'status_history' not in o:
                            o['status_history'] = []
                        o['status_history'].append({
                            'timestamp': datetime.datetime.now().isoformat(),
                            'status': new_status,
                            'notes': notes
                        })
            
            st.success(f" Status updated to: {new_status}")
            st.rerun()
    
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# render_backoffice_dashboard, render_backoffice_management, and
# run_system_health_check live in backoffice.py and backoffice_helpers.py
# They are NOT duplicated here — import from those modules instead.
