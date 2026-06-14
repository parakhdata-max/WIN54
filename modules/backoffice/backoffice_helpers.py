"""
Backoffice Helpers Module
Utility functions, formatters, and calculations

Contains:
- Formatting functions (fmt_num, fmt_signed)
- Power calculations (compute_jobcard_power)
- Stock batch resolution
- Widget key management
- Price lookups
- Order categorization
- Database loading
"""

import streamlit as st
import pandas as pd
import datetime
import logging
import math
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


import re as _uuid_re

_UUID_PATTERN = _uuid_re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _uuid_re.IGNORECASE,
)

def _safe_uuid(val: str) -> str:
    v = str(val or "").strip()
    return v if _UUID_PATTERN.match(v) else ""

def _order_no_from_id(val: str) -> str:
    v = str(val or "").strip()
    return v if v and not _UUID_PATTERN.match(v) else ""


def _dict_from_json(value) -> Dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def apply_schemes_to_order_lines(order: Dict, party_id: str = "") -> int:
    """
    Apply active supplier/own-product schemes to all order lines.
    Returns count of lines where scheme was applied.
    """
    modified = 0
    try:
        from modules.pricing.supplier_scheme_engine import apply_customer_scheme_to_line
    except ImportError:
        return 0
    _pid = party_id or str(order.get("party_id") or order.get("customer_id") or "")
    _otype = str(order.get("order_type") or "WHOLESALE").upper()
    for line in order.get("lines", []):
        if bool(line.get("is_deleted")) or bool(line.get("is_service_line")):
            continue
        if _price_locked(line):
            continue
        if not float(line.get("unit_price") or 0):
            continue
        try:
            old_p = float(line.get("unit_price") or 0)
            updated = apply_customer_scheme_to_line(line, party_id=_pid, order_type=_otype)
            new_p = float(updated.get("unit_price") or old_p)
            if abs(new_p - old_p) > 0.001:
                line.update(updated)
                modified += 1
        except Exception as _se:
            import logging
            logging.getLogger(__name__).debug("[scheme] %s", _se)
    return modified


def apply_cart_schemes_to_order_lines(order: Dict, party_id: str = "") -> int:
    """
    Apply cart-level offers such as spectacle 1+1 and CL 12+2.
    Returns the number of affected lines stamped by the cart engine.
    """
    try:
        from modules.pricing.cart_scheme_engine import apply_cart_schemes
    except ImportError:
        return 0
    lines = order.get("lines", [])
    if not lines:
        return 0
    _pid = party_id or str(order.get("party_id") or order.get("customer_id") or "")
    _otype = str(order.get("order_type") or "WHOLESALE").upper()
    try:
        before = [
            (
                float(line.get("unit_price") or 0),
                float(line.get("billing_total") or line.get("total_price") or 0),
            )
            for line in lines
        ]
        updated, result = apply_cart_schemes(lines, party_id=_pid, order_type=_otype)
        order["lines"] = updated
        affected = 0
        for idx, line in enumerate(updated):
            old_unit, old_total = before[idx] if idx < len(before) else (0.0, 0.0)
            new_unit = float(line.get("unit_price") or 0)
            new_total = float(line.get("billing_total") or line.get("total_price") or 0)
            lp = line.get("lens_params") if isinstance(line.get("lens_params"), dict) else {}
            if (
                lp.get("cart_offer_status") == "APPLIED"
                or abs(new_unit - old_unit) > 0.001
                or abs(new_total - old_total) > 0.001
            ):
                affected += 1
        if getattr(result, "applied", False):
            order.setdefault("pricing_audit", {})["cart_scheme"] = getattr(result, "message", "")
        return affected
    except Exception as _ce:
        logging.getLogger(__name__).debug("[cart_scheme] %s", _ce)
        return 0


def _retail_cash_round(value) -> float:
    """Retail payable totals are collected at whole-rupee precision."""
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return float(round(float(value or 0)))


def _resolve_order_party_id(order: Dict) -> str:
    party_id = str(order.get("party_id") or order.get("customer_id") or "").strip()
    if party_id:
        return party_id
    party_name = str(order.get("party_name") or order.get("patient_name") or order.get("party") or "").strip()
    if not party_name:
        return ""
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT id::text AS id FROM parties "
            "WHERE party_name=%(name)s AND COALESCE(is_active, TRUE)=TRUE LIMIT 1",
            {"name": party_name},
        ) or []
        return str(rows[0].get("id") or "") if rows else ""
    except Exception:
        return ""


def _price_locked(line: Dict) -> bool:
    lp = _dict_from_json(line.get("lens_params"))
    return bool(line.get("manual_price_override")) or bool(lp.get("price_locked"))


def _restamp_cart_gst(line: Dict, order_type: str) -> None:
    """Cart schemes intentionally alter totals only; restamp GST afterwards."""
    try:
        net = float(line.get("billing_total") or line.get("total_price") or 0)
        gst_pct = float(line.get("gst_percent") or 0)
        if gst_pct <= 0 or net <= 0:
            line["gst_amount"] = 0.0
            return
        if str(order_type or "").upper() == "RETAIL":
            line["gst_amount"] = round(net - (net / (1 + gst_pct / 100)), 2)
        else:
            line["gst_amount"] = round(net * gst_pct / 100, 2)
    except Exception:
        pass


def persist_order_pricing_state(order: Dict) -> int:
    """Persist current in-memory pricing fields for every live order line."""
    lines = [
        line for line in (order.get("lines") or [])
        if not bool(line.get("is_deleted"))
        and str(line.get("line_id") or line.get("id") or "").strip()
    ]
    if not lines:
        return 0

    try:
        from modules.sql_adapter import run_transaction
    except Exception:
        return 0

    steps = []
    for line in lines:
        lid = str(line.get("line_id") or line.get("id") or "").strip()
        lp = _dict_from_json(line.get("lens_params"))
        total = float(line.get("billing_total") or line.get("total_price") or 0)
        steps.append((
            """
            UPDATE order_lines
            SET unit_price       = %(up)s,
                total_price      = %(tp)s,
                billing_total    = %(tp)s,
                gst_percent      = %(gp)s,
                gst_amount       = %(ga)s,
                discount_percent = %(dp)s,
                discount_amount  = %(da)s,
                discount_rule    = %(dr)s,
                applied_rule_ids = %(ari)s,
                lens_params      = %(lp)s::jsonb
            WHERE id = %(lid)s::uuid
            """,
            {
                "lid": lid,
                "up": float(line.get("unit_price") or 0),
                "tp": total,
                "gp": float(line.get("gst_percent") or 0),
                "ga": float(line.get("gst_amount") or 0),
                "dp": float(line.get("discount_percent") or 0),
                "da": float(line.get("discount_amount") or 0),
                "dr": str(line.get("discount_rule") or ""),
                "ari": str(line.get("applied_rule_ids") or ""),
                "lp": json.dumps(lp),
            },
        ))

    order_id = str(order.get("id") or order.get("order_id") or "").strip()
    if order_id:
        total_value = sum(
            float(line.get("billing_total") or line.get("total_price") or 0)
            for line in (order.get("lines") or [])
            if not bool(line.get("is_deleted"))
        )
        if str(order.get("order_type") or "").upper() == "RETAIL":
            total_value = _retail_cash_round(total_value)
        else:
            total_value = round(total_value, 2)
        steps.append((
            "UPDATE orders SET total_value=%(tv)s, updated_at=NOW() WHERE id=%(oid)s::uuid",
            {"tv": total_value, "oid": order_id},
        ))
        order["total_value"] = total_value

    if steps:
        run_transaction(steps)
    return len(lines)


def refresh_order_pricing_rules(order: Dict, *, persist: bool = True) -> int:
    """Run the full pricing stack after backoffice product/power/qty edits.

    Order of operations is intentional:
    1. base discount rules;
    2. supplier / own-product customer schemes;
    3. cart/free schemes;
    4. GST restamp after cart schemes;
    5. optional DB persistence and header total refresh.
    """
    lines = order.get("lines") or []
    if not lines:
        return 0

    party_id = _resolve_order_party_id(order)
    order_type = str(order.get("order_type") or "RETAIL").upper()
    touched = 0

    try:
        from modules.pricing.discount_flow import apply_order_discounts
        discount_lines = [
            line for line in lines
            if not bool(line.get("is_deleted"))
            and not bool(line.get("is_service_line"))
            and not _price_locked(line)
            # Skip lines already priced by a supplier scheme —
            # scheme price is final; party/brand discount must not compound on top
            and not bool(line.get("supplier_scheme_applied"))
            and not bool(line.get("scheme_applied"))
        ]
        if discount_lines:
            apply_order_discounts(discount_lines, party_id=party_id, order_type=order_type)
            touched += len(discount_lines)
    except Exception as exc:
        logger.warning("[pricing_sync] base discount refresh failed: %s", exc)

    try:
        touched += apply_schemes_to_order_lines(order, party_id=party_id)
    except Exception as exc:
        logger.warning("[pricing_sync] supplier scheme refresh failed: %s", exc)

    try:
        touched += apply_cart_schemes_to_order_lines(order, party_id=party_id)
    except Exception as exc:
        logger.warning("[pricing_sync] cart scheme refresh failed: %s", exc)

    # Step 4: Club / same-order free product offers
    try:
        from modules.pricing.club_engine import apply_club_offers
        active_lines = [l for l in lines
                        if not bool(l.get("is_deleted"))
                        and not bool(l.get("is_service_line"))]
        if active_lines:
            apply_club_offers(active_lines,
                              order_type=order_type,
                              party_id=str(party_id or ""))
            touched += 1
    except Exception as exc:
        logger.warning("[pricing_sync] club offer refresh failed: %s", exc)

    for line in lines:
        if bool(line.get("is_deleted")):
            continue
        if bool(line.get("is_service_line")):
            continue
        _restamp_cart_gst(line, order_type)

    if persist:
        try:
            persist_order_pricing_state(order)
        except Exception as exc:
            logger.warning("[pricing_sync] persist failed: %s", exc)
    return touched


def auto_heal_zero_priced_sibling_lines(order: Dict, *, persist: bool = True) -> int:
    """Mirror price/discount from a same-product sibling when one eye is zero.

    This is a loader-level safety net for backoffice edits that accidentally
    saved one eye with unit_price=0 while the paired eye stayed correctly
    priced. It never invents a price: it only copies from another non-deleted
    line on the same order with the same product_id and a positive unit_price.
    """
    healed = 0
    lines = order.get("lines") or []
    order_type = str(order.get("order_type") or "RETAIL").upper()

    for line in lines:
        try:
            if bool(line.get("is_deleted")):
                continue
            qty = int(line.get("billing_qty") or line.get("quantity") or line.get("qty") or 0)
            if qty <= 0 or float(line.get("unit_price") or 0) > 0:
                continue
            pid = str(line.get("product_id") or "")
            if not pid:
                continue

            source = None
            for sibling in lines:
                if sibling is line or bool(sibling.get("is_deleted")):
                    continue
                if str(sibling.get("product_id") or "") != pid:
                    continue
                if float(sibling.get("unit_price") or 0) > 0:
                    source = sibling
                    break
            if not source:
                continue

            unit_price = float(source.get("unit_price") or 0)
            gst_percent = float(source.get("gst_percent") or line.get("gst_percent") or 0)
            discount_percent = float(source.get("discount_percent") or 0)
            discount_rule = str(source.get("discount_rule") or "")
            source_qty = int(source.get("billing_qty") or source.get("quantity") or 0) or qty
            if discount_percent > 0:
                discount_amount = round(unit_price * qty * discount_percent / 100, 2)
            else:
                per_qty_discount = float(source.get("discount_amount") or 0) / source_qty if source_qty else 0.0
                discount_amount = round(per_qty_discount * qty, 2)

            net_total = round(max(0.0, (unit_price * qty) - discount_amount), 2)
            if order_type == "RETAIL" and gst_percent:
                gst_amount = round(net_total * gst_percent / (100 + gst_percent), 2)
            else:
                gst_amount = round(net_total * gst_percent / 100, 2) if gst_percent else 0.0

            line["unit_price"] = unit_price
            line["gst_percent"] = gst_percent
            line["discount_percent"] = discount_percent
            line["discount_amount"] = discount_amount
            line["discount_rule"] = discount_rule
            line["total_price"] = net_total
            line["billing_total"] = net_total
            line["gst_amount"] = gst_amount
            healed += 1

            line_id = str(line.get("line_id") or line.get("id") or "")
            if persist and line_id:
                try:
                    from modules.sql_adapter import run_write
                    run_write(
                        """
                        UPDATE order_lines
                        SET unit_price=%(up)s,
                            gst_percent=%(gp)s,
                            discount_percent=%(dp)s,
                            discount_amount=%(da)s,
                            discount_rule=%(dr)s,
                            total_price=%(tp)s,
                            billing_total=%(tp)s,
                            gst_amount=%(ga)s
                        WHERE id=%(id)s::uuid
                          AND COALESCE(unit_price, 0) = 0
                        """,
                        {
                            "up": unit_price,
                            "gp": gst_percent,
                            "dp": discount_percent,
                            "da": discount_amount,
                            "dr": discount_rule,
                            "tp": net_total,
                            "ga": gst_amount,
                            "id": line_id,
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "[BO] loader price auto-heal persist failed for %s/%s: %s",
                        order.get("order_no"), line_id, exc,
                    )
            logger.warning(
                "[BO] loader price auto-healed %s line %s product_id=%s to %.2f",
                order.get("order_no"), line_id, pid, unit_price,
            )
        except Exception as exc:
            logger.warning("[BO] loader price auto-heal skipped a line: %s", exc)

    return healed


def _configured_inhouse_brands() -> set[str]:
    try:
        from modules.settings.shop_master import get_inhouse_lab_brands
        return get_inhouse_lab_brands()
    except Exception:
        return set()


def resolve_line_route(line: Dict) -> str:
    """
    Live fulfillment route resolver for backoffice cards/tabs.

    Saved route wins. If no route is saved yet, infer from real fulfillment
    signals, never from product eye/power alone.
    """
    lp = _dict_from_json(line.get("lens_params"))
    route = str(
        line.get("manufacturing_route") or
        lp.get("manufacturing_route") or ""
    ).upper().strip()

    # STOCK and INHOUSE routes are always authoritative — saved assignment wins.
    if route in ("STOCK", "INHOUSE"):
        return route

    # VENDOR/EXTERNAL_LAB saved route: override to STOCK if the product is
    # actually a stock item (frames, accessories etc.) with qty available.
    # This corrects old orders where the frame was mis-routed to VENDOR because
    # the route resolver defaulted to VENDOR before the frames fix was deployed.
    if route in ("VENDOR", "EXTERNAL_LAB"):
        _main_group = str(line.get("main_group") or "").lower()
        _stock_groups_chk = {"frames", "sunglasses", "accessories", "accessory", "stock"}
        if _main_group in _stock_groups_chk:
            # Frame/accessory with a VENDOR route saved — check if stock exists
            try:
                _pid_chk = str(line.get("product_id") or lp.get("product_id") or "").strip()
                if _pid_chk:
                    from modules.sql_adapter import run_query as _rq_chk
                    _qty_row = _rq_chk(
                        "SELECT COALESCE(SUM(quantity),0) AS total_qty "
                        "FROM inventory_stock WHERE product_id=%(p)s::uuid "
                        "AND COALESCE(is_active,TRUE)=TRUE",
                        {"p": _pid_chk},
                    )
                    _total_qty = int((_qty_row[0].get("total_qty") if _qty_row else 0) or 0)
                    if _total_qty > 0:
                        return "STOCK"  # override: stock is available, route as STOCK
            except Exception:
                pass
        return route  # non-frame VENDOR/EXTERNAL_LAB — keep as saved

    batch_status = str(line.get("batch_status") or lp.get("batch_status") or "").upper()
    batch_no = str(line.get("batch_no") or lp.get("batch_no") or "").strip()
    if batch_no or batch_status == "ALLOCATED" or line.get("batch_allocation") or lp.get("batch_allocation"):
        return "STOCK"

    try:
        allocated = int(line.get("allocated_qty") or 0)
        needed = int(line.get("billing_qty") or line.get("quantity") or 1)
        if allocated >= needed and needed > 0:
            return "STOCK"
    except Exception:
        pass

    brand = str(line.get("brand") or lp.get("brand") or "").strip().lower()
    main_group = str(line.get("main_group") or "").lower()
    lens_item_type = str(
        line.get("lens_item_type") or
        lp.get("lens_item_type") or
        lp.get("fulfillment_type") or ""
    ).upper().replace("_", " ")

    if brand and brand in _configured_inhouse_brands() and "ophthalmic" in main_group:
        return "INHOUSE"
    if "RX" in lens_item_type:
        return "VENDOR"
    if "ophthalmic" in main_group or "contact" in main_group:
        return "VENDOR"

    # Frames, accessories, sunglasses, and any other physical stock item
    # should route to STOCK when there is inventory — not VENDOR.
    # If inventory_stock has a record with qty > 0 for this product, it is
    # a stocked item regardless of main_group name.
    _stock_groups = {"frames", "sunglasses", "accessories", "accessory",
                     "contact lens", "stock", "contact lenses"}
    if main_group in _stock_groups:
        return "STOCK"

    # Final fallback: try inventory_stock lookup — any product with a stock
    # record is a STOCK item; only true custom/prescription items are VENDOR.
    try:
        _pid = str(line.get("product_id") or lp.get("product_id") or "").strip()
        if _pid:
            from modules.sql_adapter import run_query as _rq_rt
            _stk = _rq_rt(
                "SELECT 1 FROM inventory_stock "
                "WHERE product_id = %(pid)s::uuid "
                "  AND COALESCE(quantity, 0) > 0 LIMIT 1",
                {"pid": _pid},
            )
            if _stk:
                return "STOCK"
    except Exception:
        pass

    return "VENDOR"


def derive_production_ref(line: dict, order_no: str) -> str | None:
    """Return the internal production reference for an order line.

    The customer-facing order number stays unchanged. This label only tells
    production boards which lines move together and which service jobs move
    independently. Existing non-empty production_ref values are preserved by
    callers, so manual corrections remain sticky.
    """
    ref = str((line or {}).get("production_ref") or "").strip()
    if ref:
        return ref

    base = str(order_no or "").strip()
    if not base:
        return None

    lp = _dict_from_json((line or {}).get("lens_params"))
    eye = str((line or {}).get("eye_side") or "").upper().strip()
    route = str(
        (line or {}).get("manufacturing_route")
        or lp.get("manufacturing_route")
        or ""
    ).upper().strip()
    is_service = bool((line or {}).get("is_service_line")) or eye in ("S", "SERVICE")
    service_text = " ".join(
        str(x or "")
        for x in (
            lp.get("service_group"),
            lp.get("charge_type"),
            lp.get("service_type"),
            lp.get("service_production_type"),
            lp.get("service_description"),
            route,
        )
    ).upper()

    # Service production lines win over eye-side. This covers the partial-pair
    # / party-supplied-lens case where the line may still carry R/L but the
    # actual production work is colouring or fitting only.
    if is_service:
        if any(k in service_text for k in ("COLOUR", "COLOR", "TINT")):
            return f"{base}-C"
        if "FIT" in service_text:
            return f"{base}-F"
        return None

    if eye in ("R", "L", "RE", "LE", "RIGHT", "LEFT"):
        return base

    return None


def _production_ref_column_exists() -> bool:
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'order_lines'
              AND column_name = 'production_ref'
            LIMIT 1
            """
        ) or []
        return bool(rows)
    except Exception as exc:
        logger.warning("production_ref column check failed: %s", exc)
        return False


def ensure_order_production_refs(order_no: str = "", order_id: str = "") -> int:
    """Fill missing production_ref values for one order.

    Idempotent and conservative: only updates rows where production_ref IS
    NULL. Direct-bill service rows intentionally remain NULL.
    """
    if not _production_ref_column_exists():
        return 0

    try:
        from modules.sql_adapter import run_query, run_write
        rows = run_query(
            """
            SELECT
                ol.id::text AS line_id,
                ol.eye_side,
                COALESCE(ol.is_service_line, FALSE) AS is_service_line,
                ol.lens_params,
                ol.production_ref,
                COALESCE(ol.lens_params->>'manufacturing_route', '') AS lp_route,
                o.order_no
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
              AND ol.production_ref IS NULL
              AND (
                    (%(oid)s <> '' AND o.id = %(oid)s::uuid)
                 OR (%(ono)s <> '' AND o.order_no = %(ono)s)
              )
            """,
            {"oid": _safe_uuid(order_id), "ono": str(order_no or _order_no_from_id(order_id))},
        ) or []
    except Exception as exc:
        logger.warning("production_ref load failed for %s/%s: %s", order_no, order_id, exc)
        return 0

    changed = 0
    for row in rows:
        try:
            line = dict(row)
            lp = _dict_from_json(line.get("lens_params"))
            if line.get("lp_route") and "manufacturing_route" not in lp:
                lp["manufacturing_route"] = line.get("lp_route")
            line["lens_params"] = lp
            ref = derive_production_ref(line, str(row.get("order_no") or ""))
            if not ref:
                continue
            run_write(
                """
                UPDATE order_lines
                SET production_ref = %(ref)s
                WHERE id = %(lid)s::uuid
                  AND production_ref IS NULL
                """,
                {"ref": ref, "lid": str(row.get("line_id") or "")},
            )
            changed += 1
        except Exception as exc:
            logger.warning("production_ref update skipped for %s: %s", row.get("line_id"), exc)
    return changed

# Import core dependencies
from modules.power_engine import (
    vertex_correct_spherical,
    vertex_correct_toric
)
from modules.batch_manager import get_available_stock
from modules.utils.power_normalizer import normalize_power
from modules.sql_adapter import (
    read_product_master,
    fetch_backoffice_orders,
    fetch_orders_with_lines   #  ADD THIS
)

#  Lazy import to avoid circular dependency at module load time
# refresh_line_state is imported inside functions that use it below


# ====================================================================
# FORMATTING HELPERS
# ====================================================================

def safe_text(val, default: str = "") -> str:
    """Return clean text for DB/pandas values, treating None/NaN as blank."""
    if val is None:
        return default
    try:
        if isinstance(val, float) and math.isnan(val):
            return default
    except Exception:
        pass
    txt = str(val).strip()
    if txt.lower() in ("nan", "none", "nat"):
        return default
    return txt


def fmt_num(val, pattern="{:.2f}", default="N/A"):
    """Format numeric value with pattern  handles None and NaN"""
    try:
        if val is None:
            return default
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return default
        return pattern.format(f)
    except (TypeError, ValueError):
        return default


def fmt_signed(val, default="N/A"):
    """Format numeric value with +/- sign  handles None and NaN"""
    return fmt_num(val, "{:+.2f}", default)


def get_display_order_id(order: Dict) -> str:
    """
    Returns the primary order identifier used for lookups (order_no / PO-XXXX).
    NOTE: for display with the sequential number, use get_display_label(order).
    """
    return (
        order.get('final_order_id')
        or order.get('order_no')
        or order.get('order_id')
        or order.get('provisional_order_id', 'UNKNOWN')
    )


def get_display_label(order: Dict) -> str:
    """
    Human-readable order label shown in the UI.
    Shows sequential number if available: #0042 · PO-11C0DC84
    Falls back to just the order_no.
    """
    order_no = get_display_order_id(order)
    seq      = order.get("display_order_no")
    if seq:
        return f"#{int(seq):04d} · {order_no}"
    return order_no


# ====================================================================
# POWER + BATCH HELPERS (JOB CARD / STOCK)
# ====================================================================

def compute_jobcard_power(line: Dict) -> Dict:
    """
    Compute manufacturing power:
    - Contact lenses: Apply vertex correction (effectivity)
    - Ophthalmic lenses: Use spectacle power directly (no effectivity)
    """
    
    def safe_int_axis(axis_val):
        """Safely convert axis to int, handling None and NaN"""
        if axis_val is None:
            return None
        if isinstance(axis_val, float) and math.isnan(axis_val):
            return None
        try:
            return int(axis_val)
        except (ValueError, TypeError):
            return None
    
    def is_empty_power(val):
        """Check if a power value is empty (None, 0, or NaN)"""
        if val is None or val == 0:
            return True
        if isinstance(val, float) and math.isnan(val):
            return True
        return False
    
    sph = line.get('sph')
    cyl = line.get('cyl', 0)
    axis = line.get('axis')
    
    if sph is None:
        return {}
    
    # Determine product type
    main_group = safe_text(line.get('main_group')).lower()
    product_type = safe_text(line.get("type") or line.get("category") or line.get("lens_category")).lower()
    is_contact_lens = 'contact' in main_group or product_type == 'contact_lens'
    use_effective = line.get("use_effective_power", False)
    
    #  FIX: For ophthalmic lenses, NO vertex correction (use spectacle power directly)
    if not is_contact_lens:

        # Spherical (no cylinder)
        if is_empty_power(cyl):
            return {
                'sph_out': float(sph),
                'cyl_out': 0.0,
                'axis_out': None
            }

        # Toric (with cylinder)
        else:
            return {
                'sph_out': float(sph),
                'cyl_out': float(cyl),
                'axis_out': safe_int_axis(axis)
            }

    #  Contact lenses
    if is_contact_lens:

        # With effective power (vertex)
        if use_effective:

            # Spherical
            if is_empty_power(cyl):
                return {
                    'sph_out': vertex_correct_spherical(sph),
                    'cyl_out': 0.0,
                    'axis_out': None
                }

            # Toric
            sph_o, cyl_o, axis_o = vertex_correct_toric(sph, cyl, axis)

            return {
                'sph_out': sph_o,
                'cyl_out': cyl_o,
                'axis_out': axis_o
            }

        # Without effective  copy RX
        else:

            if is_empty_power(cyl):
                return {
                    'sph_out': float(sph),
                    'cyl_out': 0.0,
                    'axis_out': None
                }

            return {
                'sph_out': float(sph),
                'cyl_out': float(cyl),
                'axis_out': safe_int_axis(axis)
            }


def resolve_stock_batch(line: Dict) -> Dict:
    """
    Apply batch logic for ALL stock / job-card items
    """
    product_id = line.get('product_id')
    if not product_id:
        return {'batch_status': 'NO_PRODUCT'}

    #  Ensure product_id is string for UUID compatibility
    product_id = str(product_id)

    # -------------------------------------------------
    # Use manufacturing power first, fallback to RX
    # -------------------------------------------------
    sph = normalize_power(line.get("sph_out") or line.get("sph"))
    cyl = normalize_power(line.get("cyl_out") or line.get("cyl"))
    axis = normalize_power(line.get("axis_out") or line.get("axis"))
    add_power = normalize_power(line.get("add_power"))
    eye_side = line.get("eye_side")

    # Axis 0 must behave like NULL in DB
    if axis == 0:
        axis = None

    # -------------------------------------------------
    # Call stock engine
    # -------------------------------------------------
    stock_df = get_available_stock(
        product_id=product_id,
        sph=sph,
        cyl=cyl,
        axis=axis,
        add_power=add_power,
        eye_side=eye_side
    )

    # -------------------------------------------------
    # No stock case
    # -------------------------------------------------
    if stock_df.empty:
        return {"batch_status": "NO_STOCK"}

    row = stock_df.iloc[0]

    return {
        "batch_status": "ALLOCATED",
        "batch_no": row.get("batch_no"),
        "source": row.get("source"),
        "available_qty": row.get("available_qty")
    }

def power_key(order, line_idx, field):
    """Generate unique widget key for power fields"""
    order_id = get_display_order_id(order)
    return f"{field}_{order_id}_{line_idx}"


def sync_power_to_ui(line, line_idx, order):
    """
    Sync backend power  Streamlit widgets
    (Single source of truth)
    """

    sph_key = power_key(order, line_idx, "sph_out")
    cyl_key = power_key(order, line_idx, "cyl_out")
    axis_key = power_key(order, line_idx, "axis_out")

    st.session_state[sph_key] = float(line.get("sph_out") or 0)
    st.session_state[cyl_key] = float(line.get("cyl_out") or 0)

    axis = line.get("axis_out")
    st.session_state[axis_key] = int(axis) if axis is not None else 0


def force_power_refresh(line, line_idx, order):
    """
    Hard refresh manufacturing power widgets
    (Fixes Streamlit stale UI issue)
    """

    sph_key = power_key(order, line_idx, "sph_out")
    cyl_key = power_key(order, line_idx, "cyl_out")
    axis_key = power_key(order, line_idx, "axis_out")

    # Remove old widget state
    for k in (sph_key, cyl_key, axis_key):
        if k in st.session_state:
            del st.session_state[k]

    # Re-initialize from line
    st.session_state[sph_key] = float(line.get("sph_out") or 0)
    st.session_state[cyl_key] = float(line.get("cyl_out") or 0)

    axis = line.get("axis_out")
    st.session_state[axis_key] = int(axis) if axis is not None else 0


# ====================================================================
# PRICE LOOKUPS
# ====================================================================

def get_max_historical_price(product_id: str, sph=None, cyl=None, axis=None, add_power=None, eye_side=None) -> float:
    """
    Universal pricing logic for pending / non-stock items
    """

    try:
        product_id = str(product_id)

        # ==================================================
        # 1 Try batch prices (any power)
        # ==================================================

        stock_df = get_available_stock(
            product_id=product_id,
            sph=None,
            cyl=None,
            axis=None,
            add_power=None,
            eye_side=None
        )

        if not stock_df.empty:

            for col in ['unit_price', 'selling_price', 'price', 'rate', 'mrp']:

                if col in stock_df.columns:
                    prices = stock_df[col].dropna()
                    prices = prices[prices > 0]

                    if not prices.empty:
                        return float(prices.max())


        # ==================================================
        # 2 Product Master Fallback (ALL FIELDS)
        # ==================================================

        products_df = read_product_master()

        if not products_df.empty:

            row = products_df[
                products_df['product_id'].astype(str) == product_id
            ]

            if not row.empty:

                product = row.iloc[0]

                # Try all possible price fields
                price_fields = [
                    'selling_price',
                    'sale_price',
                    'price',
                    'rate',
                    'unit_price',
                    'mrp',
                    'mrp_price',
                    'mrp_rate'
                ]

                for field in price_fields:
                    if field in product and pd.notna(product[field]):

                        val = float(product[field])

                        if val > 0:
                            return val


        # ==================================================
        # 3 Emergency fallback: Last known order price
        # ==================================================

        from modules.sql_adapter import fetch_last_product_price

        try:
            last_price = fetch_last_product_price(product_id)

            if last_price and last_price > 0:
                return float(last_price)

        except:
            pass


        return 0.0


    except Exception as e:
        st.warning(f"Price lookup failed: {str(e)}")
        return 0.0


# ====================================================================
# DATABASE AND ORDER MANAGEMENT
# ====================================================================

def _fetch_display_numbers(order_nos: list) -> dict:
    """
    Safely fetch display_order_no for a list of orders.
    Returns {} if column doesn't exist yet (migration pending).
    """
    if not order_nos:
        return {}
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT order_no, display_order_no FROM orders "
            "WHERE order_no = ANY(%(nos)s) AND display_order_no IS NOT NULL",
            {"nos": order_nos}
        )
        return {r["order_no"]: r["display_order_no"] for r in (rows or [])}
    except Exception:
        # Column doesn't exist yet — migration hasn't run, silently skip
        return {}




@st.cache_data(ttl=30, show_spinner=False)  # WIN 1: was ttl=5 — explicit .clear() on save handles freshness
def load_orders_from_database(limit: int = 10, offset: int = 0, include_closed: bool = False, order_no: str = None):

    import time as _bo_perf_time
    _bo_perf_t0 = _bo_perf_time.perf_counter()
    try:
        from modules.core.system_observer import perf_step

        logger.info(f"[BO] Loading orders limit={limit} offset={offset} closed={include_closed} order_no={order_no or ''}")

        with perf_step(
            f"Backoffice order header fetch ({order_no or limit})",
            category="loader",
            detail=f"limit={limit} closed={include_closed} order_no={order_no or ''}",
        ):

            # ---------------------------------------------
            # 1 Fetch headers only (fast — no JOIN)
            # ---------------------------------------------
            orders_df = fetch_backoffice_orders(limit=limit, offset=offset, include_closed=include_closed, order_no=order_no)

            if orders_df is None or orders_df.empty:
                return []

            order_nos = orders_df["order_no"].tolist()

        # ---------------------------------------------
        # 2 Batch fetch ALL lines
        # ---------------------------------------------
        lines_df = fetch_orders_with_lines(order_nos)

        # ✅ FIX: Don't bail on empty lines_df — orders may have no lines yet
        # (e.g. just created). Fall back to building header-only orders from orders_df.
        if lines_df is None or lines_df.empty:
            lines_df = pd.DataFrame()

        # ---------------------------------------------
        # 3 Build product map ONCE
        # ---------------------------------------------
        products_df = read_product_master()

        product_map = {}

        if products_df is not None and not products_df.empty:
            products_df["product_id"] = products_df["product_id"].astype(str)

            for _, p in products_df.iterrows():
                product_map[str(p["product_id"])] = p.to_dict()

        # ---------------------------------------------
        # 4 Build orders in memory
        # ---------------------------------------------
        orders_map = {}

        # ✅ FIX: Pre-populate orders_map from the headers DataFrame so that
        # orders with zero lines still appear in the backoffice list.
        for _, hrow in orders_df.iterrows():
            ono = hrow.get("order_no")
            if ono and ono not in orders_map:
                _raw_status1 = hrow.get("status") or hrow.get("order_status") or "PENDING"
                # Use canonical alias map from order_status_live
                try:
                    from modules.backoffice.order_status_live import _ALIAS as _OSL_ALIAS
                    _status1 = _OSL_ALIAS.get(_raw_status1, _raw_status1)
                except Exception:
                    _status1 = ("PENDING" if _raw_status1 in ("PENDING_VALIDATION","PROVISIONAL","ORDER_SAVED")
                                else _raw_status1)
                orders_map[ono] = {
                    "id":           str(hrow.get("order_id") or ""),  # UUID for DB queries
                    "order_no":         ono,
                    "display_order_no": hrow.get("display_order_no"),
                    "created_at":       hrow.get("created_at"),
                    "order_date":       hrow.get("created_at"),
                    "status":       _status1,
                    "patient_name": hrow.get("patient_name"),
                    "party_name":   hrow.get("party_name", ""),
                    "order_type":   hrow.get("order_type", "RETAIL"),
                    "order_source": hrow.get("order_source", ""),
                    "total_value":  float(hrow.get("total_value") or 0),
                    "party_id":     str(hrow.get("party_id") or ""),
                    "patient_mobile": hrow.get("patient_mobile") or "",
                    "customer_order_no": hrow.get("customer_order_no") or "",
                    "expected_supply_date": hrow.get("expected_supply_date"),
                    "lines":        [],
                }

        for _, row in lines_df.iterrows():

            ono = row["order_no"]

            if ono not in orders_map:
                # Shouldn't happen after pre-populate, but keep as safety net
                orders_map[ono] = {
                    "id":           str(row.get("order_id") or ""),  # UUID for DB queries
                    "order_no":         ono,
                    "display_order_no": row.get("display_order_no"),
                    "created_at":       row.get("created_at"),
                    "order_date":       row.get("created_at"),
                    "status":       (lambda s: ((__import__("modules.backoffice.order_status_live", fromlist=["_ALIAS"])._ALIAS.get(s, s)) if True else s))(
                                        row.get("status") or row.get("order_status") or "PENDING"
                                    ),
                    "patient_name": row.get("patient_name"),
                    "party_name":   row.get("party_name", ""),
                    "order_type":   row.get("order_type", "RETAIL"),
                    "order_source": row.get("order_source", ""),
                    "total_value":  float(row.get("total_value") or 0),
                    "party_id":     str(row.get("party_id") or ""),
                    "patient_mobile": row.get("patient_mobile") or "",
                    "customer_order_no": row.get("customer_order_no") or "",
                    "expected_supply_date": row.get("expected_supply_date"),
                    "lines":        [],
                }

            if pd.notna(row["line_id"]):

                product_id = str(row["product_id"])
                product_meta = product_map.get(product_id, {})

                # Extract manufacturing_route from lens_params JSON
                # (stored inside lens_params JSONB, not a real column)
                def _parse_jsonb_field(val):
                    """Parse JSONB that pandas may return as str, dict, None, or NaN."""
                    import math as _math2
                    if val is None:
                        return {}
                    if isinstance(val, float) and _math2.isnan(val):
                        return {}
                    if isinstance(val, dict):
                        return val
                    if isinstance(val, str) and val.strip():
                        try:
                            import json as _jj
                            return _jj.loads(val) or {}
                        except Exception:
                            return {}
                    return {}

                _lp = _parse_jsonb_field(row.get("lens_params"))
                _bp = _parse_jsonb_field(row.get("boxing_params"))
                _mfg_route = _lp.get("manufacturing_route")
                _sup_oid   = _lp.get("supplier_order_id")

                line = {
                    "id": row["line_id"],
                    "line_id": str(row["line_id"]),   # explicit alias used by _line_key()
                    "product_id": product_id,

                    # PRODUCT MASTER HYDRATION
                    "product_name": safe_text(product_meta.get("product_name") or row.get("product_name"), "Unknown Product"),
                    "brand":        safe_text(product_meta.get("brand")        or row.get("brand")),
                    "unit":         safe_text(product_meta.get("unit"), "PCS"),
                    "box_size":     int(product_meta.get("box_size") or 1),
                    "main_group":   safe_text(product_meta.get("main_group")   or row.get("main_group")),
                    "category":     safe_text(product_meta.get("category")    or row.get("category") or
                                    product_meta.get("main_group")   or row.get("main_group")),
                    # "type" is the user-facing name for "category" (Excel column "Type" → DB "category")
                    "type":          safe_text(product_meta.get("type") or product_meta.get("category") or row.get("category")),
                    "lens_category": safe_text(product_meta.get("lens_category") or row.get("lens_category")),

                    # Power
                    "sph": row["sph"],
                    "cyl": row["cyl"],
                    "axis": row["axis"],
                    "add_power": row["add_power"],
                    "eye_side": row["eye_side"],

                    # Billing
                    "billing_qty":   row["billing_qty"],
                    "billing_total": row["billing_total"],
                    "unit_price":    float(row.get("unit_price") or 0),
                    "allocated_qty": int(row.get("allocated_qty") or 0),
                    "ready_qty":     int(row.get("ready_qty") or 0),

                    # GST — line column is authoritative, product master is fallback
                    "gst_percent": float(
                        row.get("gst_percent") or
                        row.get("product_gst_percent") or
                        product_meta.get("gst_percent") or 0
                    ),
                    "gst_amount": float(row.get("gst_amount") or 0),

                    # Routing — extracted from lens_params JSON (not a DB column)
                    "manufacturing_route": _mfg_route,
                    "supplier_order_id":   _sup_oid,

                    # Procurement proof linked from purchase_acknowledgements.
                    # Used by the Backoffice dashboard to show whether RX /
                    # supplier-routed lines are procured before billing opens.
                    "procurement_pa_id": safe_text(row.get("procurement_pa_id")),
                    "procurement_supplier_name": safe_text(row.get("procurement_supplier_name")),
                    "procurement_invoice_no": safe_text(row.get("procurement_invoice_no")),
                    "procurement_challan_no": safe_text(row.get("procurement_challan_no")),
                    "procurement_document_date": safe_text(row.get("procurement_document_date")),
                    "procurement_acknowledged_at": safe_text(row.get("procurement_acknowledged_at")),
                    "procurement_supplier_order_ref": safe_text(row.get("procurement_supplier_order_ref")),
                    "procurement_audit_status": safe_text(row.get("procurement_audit_status")),
                    "procurement_billing_status": safe_text(row.get("procurement_billing_status")),
                    "procurement_total_value": 0.0 if pd.isna(row.get("procurement_total_value")) else float(row.get("procurement_total_value") or 0),

                    # Status
                    "batch_status": row["batch_status"],
                    "lens_params":  _lp,
                    "boxing_params": _bp,

                    # ── Restore surfacing_data saved by job card ──────────
                    # Persisted under lens_params["surfacing_data"] on DB.
                    # Expose at top level so job card completion check works.
                    "surfacing_data": _lp.get("surfacing_data") or None,

                    "_needs_refresh": True,
                }

                # Compute manufacturing power immediately
                power = compute_jobcard_power(line)
                line.update(power)

                #  RUN WORKFLOW ENGINE (lazy import to avoid circular deps)
                try:
                    from .backoffice_logic import refresh_line_state, update_line_billing
                    refresh_line_state(line)
                    update_line_billing(line)   #  recalculate billing_total from fresh allocation
                except Exception as e:
                    _safe_err = str(e).encode("ascii", "backslashreplace").decode("ascii")
                    logger.warning(f"Workflow refresh failed: {_safe_err}")

                orders_map[ono]["lines"].append(line)

        # ---------------------------------------------
        # 5 Categorize + stamp GST on every line
        # ---------------------------------------------

        # Build gst_lookup once for all orders — queries product_gst_history
        def _make_gst_lookup():
            try:
                from modules.sql_adapter import run_query
                import datetime as _dt
                rows = run_query("""
                    SELECT product_id::text, gst_percent, effective_from
                    FROM product_gst_history
                    ORDER BY effective_from DESC
                """, params=None) or []

                # Build dict: product_id → [(date, gst_percent), ...] sorted DESC
                hist = {}
                for r in rows:
                    pid      = str(r.get("product_id") or "")
                    raw_date = r.get("effective_from")
                    pct      = float(r.get("gst_percent") or 0)
                    # Normalise effective_from to a date object — handles
                    # datetime, date, string (YYYY-MM-DD), and None
                    if isinstance(raw_date, _dt.datetime):
                        eff = raw_date.date()
                    elif isinstance(raw_date, _dt.date):
                        eff = raw_date
                    elif isinstance(raw_date, str):
                        try:
                            eff = _dt.date.fromisoformat(raw_date[:10])
                        except ValueError:
                            eff = None
                    else:
                        eff = None
                    hist.setdefault(pid, []).append((eff, pct))

                # Python-side sort DESC — defensive against query order changes
                for pid in hist:
                    hist[pid].sort(
                        key=lambda x: x[0] or _dt.date.min,
                        reverse=True
                    )

                def lookup(product_id, bill_date):
                    entries = hist.get(str(product_id), [])
                    # Normalise bill_date to date object
                    if isinstance(bill_date, _dt.datetime):
                        bd = bill_date.date()
                    elif isinstance(bill_date, _dt.date):
                        bd = bill_date
                    elif isinstance(bill_date, str):
                        try:
                            bd = _dt.date.fromisoformat(str(bill_date)[:10])
                        except ValueError:
                            bd = _dt.date.today()
                    else:
                        bd = _dt.date.today()

                    for eff, pct in entries:          # DESC — first match wins
                        if eff is None or eff <= bd:
                            return pct
                    return entries[-1][1] if entries else None   # oldest if none match

                return lookup

            except Exception as e:
                logger.warning(f"[BO] gst_lookup unavailable: {e}")
                return None

        gst_lookup_fn = _make_gst_lookup()

        result = []

        for order in orders_map.values():
            # Normalize first — guarantees all fields present before GST stamp
            try:
                from modules.core.order_normalizer import normalize_order
                order, norm_report = normalize_order(order)
                if norm_report.had_issues:
                    logger.info(f"[BO] Normalizer fixed fields on {order.get('order_no')}: {norm_report.summary()}")
            except Exception as _ne:
                logger.warning(f"[BO] Normalizer failed: {_ne}")
            auto_heal_zero_priced_sibling_lines(order, persist=True)

            # Apply active supplier / own-product schemes to all priced lines
            # after healing sets prices and before categorize finalises totals.
            _scheme_party = str(order.get("party_id") or order.get("customer_id") or "")
            try:
                apply_schemes_to_order_lines(order, party_id=_scheme_party)
            except Exception as _sch_e:
                import logging
                logging.getLogger(__name__).debug("[scheme_apply] %s", _sch_e)
            try:
                apply_cart_schemes_to_order_lines(order, party_id=_scheme_party)
            except Exception as _cart_e:
                import logging
                logging.getLogger(__name__).debug("[cart_scheme_apply] %s", _cart_e)

            categorize_order_lines(order)

            # Stamp GST on all lines now so UI shows correct values immediately
            try:
                from modules.pricing.tax_engine import apply_taxes
                import datetime
                all_order_lines = (
                    order.get("stock_lines", []) +
                    order.get("inhouse_lines", []) +
                    order.get("lab_order_lines", []) +
                    order.get("service_lines", []) +   # consultation/eye-testing fees
                    order.get("lines", [])
                )
                # NOTE: tax_input passes the SAME line dict objects as all_order_lines.
                # apply_taxes() mutates each line in-place (stamps gst_percent_used,
                # gst_amount, tax_inclusive, tax_hash). Those mutations propagate back
                # to stock_lines / inhouse_lines / lab_order_lines because Python
                # dicts are passed by reference — no separate copy needed.
                tax_input = {
                    "order_type": order.get("order_type", "RETAIL"),
                    "bill_date":  order.get("order_date") or datetime.date.today(),
                    "net_value":  sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in all_order_lines),
                    "lines":      all_order_lines,   # same objects — mutations are intentional
                    "gst_lookup": gst_lookup_fn,
                }
                apply_taxes(tax_input)
                # After this point: every line in order["stock_lines"] etc.
                # has gst_percent_used, gst_amount, tax_inclusive, tax_hash stamped.
            except Exception as e:
                logger.warning(f"[BO] GST stamp failed for {order.get('order_no')}: {e}")

            # ── Recompute billing_total + total_value at load time ──────────
            # For WHOLESALE: resolve selling_price via batch DB lookup (1 query
            # for all products in this order), apply box logic, recompute totals.
            # For RETAIL: reapply box logic using existing unit_price (MRP).
            _all_order_lines2 = list({
                id(l): l for src in ("stock_lines","inhouse_lines","lab_order_lines","lines")
                for l in (order.get(src) or [])
            }.values())

            _otype_e = (order.get("order_type") or "RETAIL").upper()

            # For WHOLESALE: batch-fetch selling_price for all product_ids once
            _sp_map = {}
            if _otype_e == "WHOLESALE" and _all_order_lines2:
                try:
                    from modules.sql_adapter import run_query as _rqbatch
                    _pids = list({str(l.get("product_id") or "") for l in _all_order_lines2 if l.get("product_id")})
                    if _pids:
                        # inventory_stock.selling_price (batch-level wholesale price)
                        _inv_rows = _rqbatch("""
                            SELECT DISTINCT ON (product_id)
                                   product_id::text, selling_price
                            FROM inventory_stock
                            WHERE product_id::text = ANY(%(pids)s)
                              AND is_active = true
                              AND selling_price IS NOT NULL
                              AND selling_price > 0
                            ORDER BY product_id, created_at DESC
                        """, {"pids": _pids}) or []
                        for _r in _inv_rows:
                            _sp_map[str(_r["product_id"])] = float(_r["selling_price"])

                        # Fill gaps from inventory prices available on older rows.
                        # Products table does not carry selling_price/unit_price in this schema.
                        _missing = [p for p in _pids if p not in _sp_map]
                        if _missing:
                            _prod_rows = _rqbatch("""
                                SELECT DISTINCT ON (product_id)
                                       product_id::text AS id,
                                       COALESCE(selling_price, mrp, purchase_rate, 0) AS price
                                FROM inventory_stock
                                WHERE product_id::text = ANY(%(pids)s)
                                  AND COALESCE(is_active, true) = true
                                  AND COALESCE(selling_price, mrp, purchase_rate, 0) > 0
                                ORDER BY product_id, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                            """, {"pids": _missing}) or []
                            for _r in _prod_rows:
                                _v = float(_r.get("price") or 0)
                                if _v > 0:
                                    _sp_map[str(_r["id"])] = _v
                except Exception as _be:
                    logger.warning(f"[BO] Batch price lookup failed: {_be}")

            # tax_inclusive derived from order_type — NOT from line field (not persisted in DB)
            _tax_inc_e = (_otype_e == "RETAIL")

            for _el in _all_order_lines2:
                # Always stamp tax_inclusive so display functions are consistent
                _el["tax_inclusive"] = _tax_inc_e

                # ── PRICE RULE (governor) ─────────────────────────────────
                # RETAIL    → mrp        (GST-inclusive, per PCS after /box_size)
                # WHOLESALE → selling_price (ex-GST, per BOX — divide for per-PCS)
                # PURCHASE  → purchase_rate (ex-GST, per BOX)
                #
                # IMPORTANT: we NEVER overwrite unit_price in the line dict.
                # unit_price = what was agreed at time of order (historical truth).
                # We only recompute billing_total + gst_amount for the display card.
                # If DB price changed after order, the order total stays unchanged.
                # ─────────────────────────────────────────────────────────

                _upr = float(_el.get("unit_price") or 0)

                # For WHOLESALE: annotate selling_price for display reference only.
                # Do NOT overwrite unit_price — preserve historical order price.
                if _otype_e == "WHOLESALE":
                    _pid_e = str(_el.get("product_id") or "")
                    _sp_e  = _sp_map.get(_pid_e, 0.0)
                    if _sp_e > 0:
                        _el["selling_price"] = _sp_e    # display reference only
                        # Flag if current DB price differs from stored order price
                        _bsz_e   = max(1, int(_el.get("box_size") or 1))
                        _sp_pcs  = round(_sp_e / _bsz_e, 2)
                        if _upr > 0 and abs(_sp_pcs - _upr) > 0.05:
                            _el["_price_drifted"] = True
                            _el["_current_sp_pcs"] = _sp_pcs

                if _upr <= 0:
                    continue  # no price — leave as-is

                # Recompute billing_total using normalize_box_total for BOX products
                _bq    = int(_el.get("billing_qty") or _el.get("quantity") or 0)
                _gpct  = float(_el.get("gst_percent_used") or _el.get("gst_percent") or 0)
                _bsz_c = max(1, int(_el.get("box_size") or 1))

                # Reconstruct BOX price from stored per-PCS price for exact total
                _box_price_for_total = round(_upr * _bsz_c, 2)
                try:
                    from modules.core.price_qty_governor import normalize_box_total as _nbt
                    _sub = _nbt(_box_price_for_total, _bq, _el)
                except Exception:
                    _sub = round(_bq * _upr, 2)

                _is_service_e = (
                    bool(_el.get("is_service_line"))
                    or str(_el.get("eye_side") or "").upper() in ("S", "SERVICE")
                )

                if _tax_inc_e and _gpct:
                    # RETAIL: MRP includes GST — back-calculate
                    _gst = round(_sub * _gpct / (100 + _gpct), 2)
                    _el["billing_total"] = round(_sub - _gst, 2)
                    _el["gst_amount"]    = _gst
                else:
                    # WHOLESALE/PURCHASE: GST added on top
                    _gst = round(_sub * _gpct / 100, 2)
                    _el["billing_total"] = round(_sub + _gst, 2) if _is_service_e and _gpct else _sub
                    _el["gst_amount"]    = _gst

            # total_value = what customer pays (grand total incl GST) for dashboard card
            _grand_total_e = 0.0
            for _el in _all_order_lines2:
                _is_service_e = (
                    bool(_el.get("is_service_line"))
                    or str(_el.get("eye_side") or "").upper() in ("S", "SERVICE")
                )
                _bt_e = float(_el.get("billing_total") or 0)
                _ga_e = float(_el.get("gst_amount") or 0)
                if _otype_e == "RETAIL":
                    _grand_total_e += _bt_e + _ga_e
                elif _is_service_e:
                    _grand_total_e += _bt_e
                else:
                    _grand_total_e += _bt_e + _ga_e
            order["total_value"] = round(_grand_total_e, 2)
            # ─────────────────────────────────────────────────────────────────

            result.append(order)

        # Bulk-fetch sequential display numbers (safe — no crash if column missing)
        _all_onos = [o.get("order_no") for o in result if o.get("order_no")]
        _disp_map = _fetch_display_numbers(_all_onos)
        for o in result:
            ono = o.get("order_no")
            if ono and ono in _disp_map:
                o["display_order_no"] = _disp_map[ono]

        logger.info(f"[BO] Loaded {len(result)} orders (BATCH MODE + Hydrated + GST stamped)")

        return result

    except Exception as e:
        logger.exception("[BO] Batch load failed")
        st.error("Backoffice load failed — check logs")
        return []
    finally:
        try:
            from modules.core.system_observer import add_perf_step
            add_perf_step(
                f"Backoffice full order hydration ({order_no or limit})",
                _bo_perf_time.perf_counter() - _bo_perf_t0,
                category="loader",
                detail=f"limit={limit} closed={include_closed} order_no={order_no or ''}",
            )
        except Exception:
            pass


# Clear stale cache on module reload (e.g. after deploy)
try:
    load_orders_from_database.clear()
except Exception:
    pass


# ====================================================================
# POWER VALIDATION (F1)
# ====================================================================

#: Backoffice power field rules — mirrors retail_punching_rx._RX_RULES
#: but uses the wider backoffice limits (±30 SPH, ±10 CYL).
_BO_POWER_RULES = {
    "sph":  {"min": -30.0, "max": +30.0, "label": "SPH"},
    "cyl":  {"min": -10.0, "max": +10.0, "label": "CYL"},
    "axis": {"min":   1,   "max":  180,  "label": "AXIS", "is_axis": True},
    "add":  {"min": +0.50, "max": +4.00, "label": "ADD"},
}


def validate_backoffice_power(
    sph=None, cyl=None, axis=None, add_power=None
) -> list:
    """
    Validate power fields for backoffice power edits (F1 fix).

    Returns a list of human-readable error strings.
    An empty list means all supplied values are valid.

    Usage in backoffice_panels.py (inside the power save block):

        from modules.backoffice.backoffice_helpers import validate_backoffice_power
        errs = validate_backoffice_power(sph, cyl, axis, add_power)
        for err in errs:
            st.error(err)
        if errs:
            st.stop()   # block the DB write
    """
    errors = []

    def _chk(val, field):
        if val is None:
            return
        rules = _BO_POWER_RULES[field]
        try:
            v = float(val)
        except (TypeError, ValueError):
            errors.append(f"{rules['label']}: '{val}' is not a valid number")
            return
        if rules.get("is_axis"):
            v = int(round(v))
            if not (rules["min"] <= v <= rules["max"]):
                errors.append(
                    f"{rules['label']}: {v}\u00b0 is out of range "
                    f"({int(rules['min'])}\u00b0\u2013{int(rules['max'])}\u00b0)"
                )
        else:
            r4 = round(v * 4) / 4
            if abs(r4 - v) > 0.001:
                errors.append(
                    f"{rules['label']}: {v:+.3f} is not a 0.25-step value "
                    f"(nearest: {r4:+.2f})"
                )
                return
            v = r4
            if not (rules["min"] <= v <= rules["max"]):
                errors.append(
                    f"{rules['label']}: {v:+.2f} out of range "
                    f"({rules['min']:+.2f} to {rules['max']:+.2f})"
                )

    _chk(sph,       "sph")
    _chk(cyl,       "cyl")
    _chk(axis,      "axis")
    _chk(add_power, "add")
    return errors


# ====================================================================
# BATCH RESERVATION UPDATE (F4)
# ====================================================================

def update_batch_reserved_qty(
    old_stock_id: str,
    new_stock_id: str,
    qty: int,
) -> bool:
    """
    Transfer a batch reservation when the batch on an order line is
    changed in the backoffice allocation editor (F4 fix).

    Call this in backoffice_panels.py immediately after writing the new
    batch to order_lines:

        from modules.backoffice.backoffice_helpers import update_batch_reserved_qty
        ok = update_batch_reserved_qty(old_stock_id, new_stock_id, qty)
        if not ok:
            st.warning(
                "Batch reserved_qty update failed — stock counts may drift. "
                "Run a stock reconciliation."
            )

    Returns True if both sides succeeded, False otherwise.
    No-op if old and new stock_id are the same.
    """
    if not old_stock_id or not new_stock_id or qty <= 0:
        return False
    if str(old_stock_id) == str(new_stock_id):
        return True  # same batch — nothing to transfer

    try:
        from modules.sql_adapter import run_write
        run_write(
            """
            UPDATE inventory_stock
            SET reserved_qty = GREATEST(COALESCE(reserved_qty, 0) - %(qty)s, 0),
                updated_at   = NOW()
            WHERE id = %(sid)s::uuid
            """,
            {"qty": qty, "sid": str(old_stock_id)},
        )
        run_write(
            """
            UPDATE inventory_stock
            SET reserved_qty = COALESCE(reserved_qty, 0) + %(qty)s,
                updated_at   = NOW()
            WHERE id = %(sid)s::uuid
            """,
            {"qty": qty, "sid": str(new_stock_id)},
        )
        logger.info(
            "[BO] Batch reserved_qty transferred: old=%s new=%s qty=%s",
            str(old_stock_id)[:8], str(new_stock_id)[:8], qty,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[BO] update_batch_reserved_qty failed (old=%s new=%s qty=%s): %s",
            str(old_stock_id)[:8], str(new_stock_id)[:8], qty, exc,
        )
        return False


def categorize_order_lines(order: Dict) -> None:
    """
    Categorizes order lines into stock_lines / inhouse_lines / lab_order_lines.

    Priority:
      1. manufacturing_route field (set by workflow engine at punching time)
         STOCK        → stock_lines
         INHOUSE      → inhouse_lines
         VENDOR       → lab_order_lines  (supplier PO route)
         EXTERNAL_LAB → lab_order_lines
      2. batch_status fallback (for old orders that pre-date routing)
         ALLOCATED    → stock_lines
      3. main_group heuristic (last resort)
         ophthalmic / contact → inhouse_lines
         everything else      → lab_order_lines
    """
    stock_lines    = []
    inhouse_lines  = []
    lab_order_lines = []

    service_lines = []

    for line in order.get("lines", []):
        eye_side     = safe_text(line.get("eye_side")).upper()
        route        = resolve_line_route(line)
        batch_status = safe_text(line.get("batch_status")).upper()
        main_group   = safe_text(line.get("main_group")).lower()

        # ── SERVICE lines (consultation fee, eye testing) ────────────────
        # Never goes to production, allocation, or supplier routing.
        # Kept in a separate bucket and added to all_lines at save time.
        if eye_side in ("SERVICE", "S") or line.get("is_service_line"):
            service_lines.append(line)
            continue

        if route == "STOCK":
            stock_lines.append(line)
        elif route == "INHOUSE":
            inhouse_lines.append(line)
        elif route in ("VENDOR", "EXTERNAL_LAB"):
            lab_order_lines.append(line)
        # ── fallback: no route saved yet ─────────────────────────────────
        elif batch_status == "ALLOCATED":
            stock_lines.append(line)
        elif "ophthalmic" in main_group or "contact" in main_group:
            lab_order_lines.append(line)
        else:
            lab_order_lines.append(line)

    order["stock_lines"]     = stock_lines
    order["inhouse_lines"]   = inhouse_lines
    order["lab_order_lines"] = lab_order_lines
    order["service_lines"]   = service_lines  # consultation/eye-testing fees


# ====================================================================
# SYSTEM DIAGNOSTICS
# ====================================================================

def run_system_health_check(order):
    """
     DIAGNOSTIC: Validate order data consistency
    """
    
    issues = []
    
    # Check order lines exist
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    
    if not all_lines:
        issues.append(" No order lines found")
    
    # Check each line
    for idx, line in enumerate(all_lines):
        
        # Check power values
        if line.get('sph') is None:
            issues.append(f"Line {idx}: Missing SPH value")
        
        if line.get('sph_out') is None:
            issues.append(f"Line {idx}: Missing manufacturing power (sph_out)")
        
        # Check allocation
        if line.get('batch_status') == 'ALLOCATED' and not line.get('batch_allocation'):
            issues.append(f"Line {idx}: Status is ALLOCATED but no batch_allocation")
        
        # Check billing
        if line.get('billing_qty', 0) > 0 and line.get('billing_total', 0) == 0:
            issues.append(f"Line {idx}: Quantity > 0 but billing_total = 0")

    # Stock allocation drift check (detect only — no fix during health check)
    try:
        from modules.backoffice.audit_logger import reconcile_stock_allocations
        _recon = reconcile_stock_allocations(fix=False)
        for _dr in (_recon.get("drifted") or []):
            issues.append(
                f"Stock drift: product {str(_dr.get('product_id',''))[:8]}… "
                f"batch={_dr.get('batch_no','?')} "
                f"drift={_dr.get('drift',0)} pcs"
            )
    except Exception:
        pass  # health check never blocks

    # Display results
    if issues:
        with st.expander(" System Health Check - Issues Found", expanded=True):
            for issue in issues:
                st.error(issue) if "billing_total = 0" in issue else st.warning(issue)
    else:
        st.success(" System Health Check: All OK")
