"""
modules/pricing/shadow_mode.py

Shadow Pricing Mode
===================
Runs the NEW pricing engine silently alongside the LEGACY engine.
Legacy engine always wins for billing — new engine output is logged only.

FEATURE FLAG:
    Set env var or session flag:
        PRICING_SHADOW_MODE = "true"   → shadow runs
        PRICING_SHADOW_MODE = "false"  → shadow skipped (default)

INTEGRATION POINT (finalize_engine.py Step 4):
    # existing line — DO NOT CHANGE:
    from modules.core.pricing_pipeline import run_pricing
    cart_lines, pricing_trace = run_pricing(cart_lines, order_info, ...)

    # add ONE line after:
    from modules.pricing.shadow_mode import run_shadow
    run_shadow(cart_lines, order_info, pricing_trace)

That's the entire integration. Nothing else in finalize_engine changes.

DB TABLE:
    pricing_shadow_decisions  (created by 004_shadow_decisions.sql)

ZERO BILLING RISK:
    - Shadow runs in a try/except
    - Any new engine failure is logged, never raised
    - Legacy trace is returned unchanged
"""

from __future__ import annotations
import os
import logging
import copy
from typing import Optional

logger = logging.getLogger(__name__)

# ── Feature flag ─────────────────────────────────────────────────────────────

def shadow_enabled() -> bool:
    """
    Check if shadow mode is active.
    Controlled by environment variable — no code change needed to toggle.

    Enable:   set PRICING_SHADOW_MODE=true   (Windows: setx PRICING_SHADOW_MODE true)
    Disable:  set PRICING_SHADOW_MODE=false  (or just unset it)
    """
    return os.environ.get("PRICING_SHADOW_MODE", "false").lower() == "true"


# ── Main entry — called from finalize_engine ─────────────────────────────────

def run_shadow(
    legacy_lines:  list,
    order_info:    dict,
    legacy_trace,                   # PricingTrace from core/pricing_pipeline
    channel:       str = "retail",
    namespace:     str = "default",
) -> None:
    """
    Run the new engine on the same cart, log delta to DB.
    Never raises — all failures are swallowed and logged.
    Legacy billing is never affected.

    Args:
        legacy_lines:  Cart lines AFTER legacy pricing (has unit_price, total_price).
        order_info:    Order header dict (same one passed to run_pricing).
        legacy_trace:  PricingTrace returned by run_pricing() — for cart totals.
        channel:       Passed to build_live_engine().
        namespace:     Passed to build_live_engine().
    """
    if not shadow_enabled():
        return

    try:
        _run_shadow_inner(legacy_lines, order_info, legacy_trace, channel, namespace)
    except Exception as exc:
        # Shadow must NEVER crash finalize. Log and move on.
        logger.error("Shadow pricing failed (non-fatal): %s", exc, exc_info=True)


# ── Internal ─────────────────────────────────────────────────────────────────

def _run_shadow_inner(legacy_lines, order_info, legacy_trace, channel, namespace):
    from modules.pricing.live_adapter import build_live_engine
    from modules.pricing.discount_rule import LineItem, SalesChannel
    from decimal import Decimal

    order_id   = order_info.get("provisional_order_id") or "PENDING"
    order_type = order_info.get("order_type", "RETAIL")
    party      = order_info.get("party") or order_info.get("patient_name", "")

    # Build new engine from live DB policy
    try:
        engine, policy = build_live_engine(channel=channel, namespace=namespace)
    except Exception as exc:
        logger.warning("Shadow: could not load live engine — %s", exc)
        _write_cart_error(order_id, order_type, party, str(exc), legacy_trace)
        return

    policy_id  = str(policy.get("id", ""))
    policy_ver = str(policy.get("version", ""))

    records = []

    for i, line in enumerate(legacy_lines):
        legacy_unit  = float(line.get("unit_price",  0) or 0)
        legacy_total = float(line.get("total_price", 0) or 0)
        qty          = int(line.get("billing_qty", 1) or 1)
        base_price   = legacy_unit or (legacy_total / qty if qty else 0)

        new_unit  = None
        new_total = None
        new_rule  = None
        new_disc  = None
        new_margin= None
        error_msg = None

        try:
            item = LineItem(
                base_price  = Decimal(str(base_price)),
                quantity    = qty,
                product_id  = line.get("product_id"),
                product_cat = line.get("product_cat") or line.get("category"),
                party_id    = line.get("party_id"),
                brand_group = line.get("brand_group"),
                channel     = _resolve_channel(order_type),
                namespace   = namespace,
            )
            result    = engine.calculate(item)
            new_unit  = float(result.net_amount / qty) if qty else 0.0
            new_total = float(result.net_amount)
            new_rule  = result.rule_name
            new_disc  = float(result.discount_pct)
            new_margin= result.margin_status

        except Exception as exc:
            error_msg = str(exc)
            logger.debug("Shadow: new engine error on line %s — %s", i, exc)

        # Price delta percentage (guard division by zero)
        if legacy_total and new_total is not None:
            delta_pct = round((new_total - legacy_total) / legacy_total * 100, 4)
        else:
            delta_pct = None

        records.append({
            "order_id":           order_id,
            "order_type":         order_type,
            "party":              party,
            "line_id":            line.get("line_id", f"line_{i}"),
            "product_name":       line.get("product_name", f"Line {i+1}"),
            "billing_qty":        qty,
            "legacy_unit_price":  legacy_unit,
            "legacy_total_price": legacy_total,
            "legacy_rule_applied":line.get("pricing_source"),
            "new_unit_price":     new_unit,
            "new_total_price":    new_total,
            "new_rule_applied":   new_rule,
            "new_discount_pct":   new_disc,
            "new_margin_status":  new_margin,
            "price_delta_pct":    delta_pct,
            "legacy_final_value": float(legacy_trace.final_value) if legacy_trace else None,
            "new_final_value":    new_total,   # per-line; cart total written on first row
            "shadow_policy_id":   policy_id,
            "shadow_policy_ver":  policy_ver,
            "new_engine_error":   error_msg,
        })

    _write_records(records)


def _resolve_channel(order_type: str):
    from modules.pricing.discount_rule import SalesChannel
    mapping = {
        "RETAIL":     SalesChannel.RETAIL,
        "WHOLESALE":  SalesChannel.WHOLESALE,
        "ONLINE":     SalesChannel.ONLINE,
    }
    return mapping.get(order_type.upper(), SalesChannel.ALL)


def _write_records(records: list) -> None:
    """Write shadow records to DB. Fire-and-forget — never raises."""
    if not records:
        return
    try:
        from modules.sql_adapter import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.executemany("""
            INSERT INTO pricing_shadow_decisions (
                order_id, order_type, party,
                line_id, product_name, billing_qty,
                legacy_unit_price, legacy_total_price, legacy_rule_applied,
                new_unit_price, new_total_price, new_rule_applied,
                new_discount_pct, new_margin_status,
                price_delta_pct,
                legacy_final_value, new_final_value,
                shadow_policy_id, shadow_policy_ver,
                new_engine_error
            ) VALUES (
                %(order_id)s, %(order_type)s, %(party)s,
                %(line_id)s, %(product_name)s, %(billing_qty)s,
                %(legacy_unit_price)s, %(legacy_total_price)s, %(legacy_rule_applied)s,
                %(new_unit_price)s, %(new_total_price)s, %(new_rule_applied)s,
                %(new_discount_pct)s, %(new_margin_status)s,
                %(price_delta_pct)s,
                %(legacy_final_value)s, %(new_final_value)s,
                %(shadow_policy_id)s::uuid, %(shadow_policy_ver)s,
                %(new_engine_error)s
            )
        """, records)
        conn.commit()
        logger.debug("Shadow: wrote %d records for order %s",
                     len(records), records[0]["order_id"])
    except Exception as exc:
        logger.error("Shadow DB write failed (non-fatal): %s", exc)


def _write_cart_error(order_id, order_type, party, error, legacy_trace) -> None:
    """Write a single error row when engine itself fails to load."""
    _write_records([{
        "order_id":           order_id,
        "order_type":         order_type,
        "party":              party,
        "line_id":            None,
        "product_name":       None,
        "billing_qty":        None,
        "legacy_unit_price":  None,
        "legacy_total_price": None,
        "legacy_rule_applied":None,
        "new_unit_price":     None,
        "new_total_price":    None,
        "new_rule_applied":   None,
        "new_discount_pct":   None,
        "new_margin_status":  None,
        "price_delta_pct":    None,
        "legacy_final_value": float(legacy_trace.final_value) if legacy_trace else None,
        "new_final_value":    None,
        "shadow_policy_id":   None,
        "shadow_policy_ver":  None,
        "new_engine_error":   error,
    }])
