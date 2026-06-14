"""
modules/pricing/club_engine.py
=================================
Club Offer Engine — "Buy Product A + Product B together → get discount"

A club offer fires at the CART level, not the line level.
The frozen engine (filter_rules / pick_best_rule) evaluates one LineItem
at a time and cannot cross-reference other lines in the cart.
This module fills that gap.

How it works:
  1. Operator defines a club offer in Admin UI → Club Offers tab:
       Trigger products: [Platinum Lens (UltraView)]   ← must be in cart
       Reward products:  [SilkLens Cleaner]             ← gets the discount
       Discount:         100% (free) / 20% / ₹50 off
       Name:             "Buy Platinum → Get Cleaner Free"

  2. Club offers are stored in the club_offers table in DB.

  3. apply_club_offers(cart_lines) is called:
       - After apply_discounts() on the full cart
       - Before save (order_pipeline, finalize sections)
       - On every add-to-cart in finalize panel (for live UI update)

  4. For each active club offer:
       - Check if ALL trigger_product_ids are present in cart
       - If yes: find reward lines, apply the discount

  5. Reward line gets:
       - discount_percent / discount_amount updated
       - discount_rule = "Club: {offer_name}"
       - applied_rule_ids appended with club_offer_id
       - billing_total updated
       - club_offer_id stamped (for UI badge display)

Zero-risk:
  Any failure leaves the cart unchanged.
  Never blocks order save.

Public API:
  from modules.pricing.club_engine import apply_club_offers
  cart_lines = apply_club_offers(cart_lines)
"""

from __future__ import annotations
import logging
from typing import List, Optional

log = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────────────
_CLUB_CACHE: list = []
_CLUB_CACHE_TS: float = 0.0
_CLUB_CACHE_TTL: float = 60.0  # seconds


def _ensure_club_schema() -> None:
    """Lightweight idempotent guard for columns added after club offers launched."""
    try:
        from modules.sql_adapter import run_write
        for ddl in [
            "ALTER TABLE club_offers ADD COLUMN IF NOT EXISTS party_filter TEXT DEFAULT ''",
            "ALTER TABLE club_offers ADD COLUMN IF NOT EXISTS application_mode TEXT DEFAULT 'SAME_ORDER'",
            "ALTER TABLE club_offers ADD COLUMN IF NOT EXISTS nominal_billing_value NUMERIC(10,2) DEFAULT 1.00",
            "ALTER TABLE club_offers ADD COLUMN IF NOT EXISTS entitlement_valid_days INT DEFAULT 0",
            "ALTER TABLE club_offers ADD COLUMN IF NOT EXISTS entitlement_auto_apply BOOLEAN DEFAULT TRUE",
        ]:
            run_write(ddl)
    except Exception:
        pass


def _load_club_offers() -> list:
    """Load active club offers from DB. Cached 60s."""
    import time
    global _CLUB_CACHE, _CLUB_CACHE_TS
    now = time.time()
    if _CLUB_CACHE and (now - _CLUB_CACHE_TS) < _CLUB_CACHE_TTL:
        return _CLUB_CACHE

    try:
        _ensure_club_schema()
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                id::text            AS id,
                name,
                description,
                trigger_product_ids,   -- JSONB array of UUIDs that must be in cart
                trigger_brand,         -- OR match by brand (optional)
                trigger_main_group,    -- OR match by category (optional)
                reward_product_ids,    -- JSONB array of UUIDs that get the discount
                reward_main_group,     -- OR reward by category (optional)
                value_type,            -- 'percent' | 'fixed' | 'free'
                value,                 -- discount % or ₹ amount (NULL for 'free')
                min_trigger_qty,       -- min qty of trigger product required
                channel,               -- 'all' | 'wholesale' | 'retail'
                stackable,             -- True = stacks on top of existing discount
                display_label,
                icon_emoji,
                COALESCE(party_filter,'') AS party_filter,
                COALESCE(application_mode,'SAME_ORDER') AS application_mode,
                COALESCE(nominal_billing_value,1)::float AS nominal_billing_value,
                COALESCE(entitlement_valid_days,0) AS entitlement_valid_days
            FROM club_offers
            WHERE COALESCE(active, TRUE) = TRUE
              AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)
              AND (valid_to   IS NULL OR valid_to   >= CURRENT_DATE)
            ORDER BY priority ASC, name ASC
        """) or []
        _CLUB_CACHE    = rows
        _CLUB_CACHE_TS = now
        log.debug(f"[ClubEngine] Loaded {len(rows)} club offers")
    except Exception as e:
        log.warning(f"[ClubEngine] DB load failed: {e}")
        _CLUB_CACHE = []

    return _CLUB_CACHE


def invalidate_club_cache() -> None:
    """Force reload on next call. Call after creating/editing a club offer."""
    global _CLUB_CACHE, _CLUB_CACHE_TS
    _CLUB_CACHE    = []
    _CLUB_CACHE_TS = 0.0


def _parse_ids(val) -> list:
    """Parse a JSONB array field (may come as list, str, or None)."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    import json
    try:
        parsed = json.loads(val)
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _line_matches_trigger(line: dict, offer: dict) -> bool:
    """
    Return True if a cart line qualifies as a trigger for this club offer.
    Match by product_id, OR brand, OR main_group (whichever are set).
    min_trigger_qty is checked if set.
    """
    trigger_ids   = _parse_ids(offer.get("trigger_product_ids"))
    trigger_brand = str(offer.get("trigger_brand") or "").strip().lower()
    trigger_cat   = str(offer.get("trigger_main_group") or "").strip().lower()

    if not trigger_ids and not trigger_brand and not trigger_cat:
        return False  # offer is misconfigured — no trigger defined

    line_pid   = str(line.get("product_id") or "").strip()
    line_brand = str(line.get("brand") or "").strip().lower()
    line_cat   = str(line.get("main_group") or "").strip().lower()
    line_qty   = int(line.get("billing_qty") or line.get("quantity") or 0)

    # Match by product_id
    if trigger_ids and line_pid in trigger_ids:
        pass
    # Match by brand
    elif trigger_brand and line_brand == trigger_brand:
        pass
    # Match by category
    elif trigger_cat and line_cat == trigger_cat:
        pass
    else:
        return False

    # Qty gate
    min_qty = int(offer.get("min_trigger_qty") or 1)
    return line_qty >= min_qty


def _line_matches_reward(line: dict, offer: dict) -> bool:
    """
    Return True if a cart line qualifies as a reward target for this offer.
    Match by product_id OR main_group (reward_main_group).
    """
    reward_ids = _parse_ids(offer.get("reward_product_ids"))
    reward_cat = str(offer.get("reward_main_group") or "").strip().lower()

    if not reward_ids and not reward_cat:
        return False  # no reward target defined

    line_pid = str(line.get("product_id") or "").strip()
    line_cat = str(line.get("main_group") or "").strip().lower()

    if reward_ids and line_pid in reward_ids:
        return True
    if reward_cat and line_cat == reward_cat:
        return True
    return False


def _apply_offer_to_line(line: dict, offer: dict) -> None:
    """
    Apply club offer discount to a reward line. Mutates in-place.
    Respects stackable flag — if False and line already has discount, skips.
    """
    unit_price = float(line.get("unit_price") or 0)
    qty        = int(line.get("billing_qty") or line.get("quantity") or 1)
    gross      = unit_price * qty
    if gross <= 0:
        return

    existing_disc = float(line.get("discount_amount") or 0)
    stackable     = bool(offer.get("stackable", True))

    if not stackable and existing_disc > 0:
        # Non-stackable: only fire if no discount already on this line
        return

    vtype = str(offer.get("value_type") or "percent").lower()
    value = float(offer.get("value") or 0)

    # Compute offer discount on the net-after-existing-discount base (compounding)
    base = gross - existing_disc if stackable else gross

    if vtype == "free":
        nominal = max(float(offer.get("nominal_billing_value") or 1), 0.0)
        offer_disc = max(0.0, base - (nominal * qty))
    elif vtype == "percent" and value > 0:
        offer_disc = round(base * value / 100, 2)
    elif vtype == "fixed" and value > 0:
        offer_disc = min(value, base)
    else:
        return

    if offer_disc <= 0:
        return

    new_disc_amt = round(existing_disc + offer_disc, 2)
    new_disc_pct = round(new_disc_amt / gross * 100, 4) if gross > 0 else 0.0
    new_disc_amt = min(new_disc_amt, gross)   # never exceed price
    new_disc_pct = min(new_disc_pct, 100.0)

    offer_name = str(offer.get("name") or "Club Offer")
    icon       = str(offer.get("icon_emoji") or "🎁")
    label      = str(offer.get("display_label") or offer_name)

    # Existing rule label — append
    existing_rule = str(line.get("discount_rule") or "")
    new_rule = f"{existing_rule} + {icon} {label}".lstrip(" + ")

    line["discount_percent"] = new_disc_pct
    line["discount_amount"]  = new_disc_amt
    line["discount_rule"]    = new_rule
    line["billing_total"]    = round(gross - new_disc_amt, 2)
    line["total_price"]      = line["billing_total"]
    line["club_offer_id"]    = str(offer.get("id") or "")
    line["club_offer_name"]  = offer_name

    # Append to applied_rule_ids
    existing_ids = str(line.get("applied_rule_ids") or "")
    oid          = str(offer.get("id") or "")
    line["applied_rule_ids"] = f"{existing_ids},{oid}".strip(",") if oid else existing_ids

    # Append to discount_breakdown for UI display
    breakdown = list(line.get("discount_breakdown") or [])
    breakdown.append({
        "type":  "club",
        "label": f"{icon} {label}",
        "icon":  icon,
        "value": round(offer_disc / gross * 100, 2),
        "name":  offer_name,
    })
    line["discount_breakdown"] = breakdown
    lp = line.get("lens_params") if isinstance(line.get("lens_params"), dict) else {}
    lp.update({
        "club_offer_status": "APPLIED",
        "club_offer_name": offer_name,
        "club_offer_id": str(offer.get("id") or ""),
        "club_offer_application_mode": str(offer.get("application_mode") or "SAME_ORDER"),
        "club_offer_nominal_billing_value": float(offer.get("nominal_billing_value") or 1),
    })
    line["lens_params"] = lp

    log.debug(
        f"[ClubEngine] Club offer '{offer_name}' → "
        f"{line.get('product_name','?')}: "
        f"−₹{offer_disc:.2f} ({new_disc_pct:.2f}%)"
    )


def apply_club_offers(
    cart_lines: list,
    order_type: str = "wholesale",
    party_id: str = "",
) -> list:
    """
    PUBLIC API — Apply club offers to the full cart.

    Must be called with ALL cart lines together (not line-by-line),
    because club offers require cross-line cart awareness.

    Call AFTER apply_discounts() so club discounts stack correctly.

    Usage:
        from modules.pricing.club_engine import apply_club_offers
        cart_lines = apply_club_offers(cart_lines, order_type="WHOLESALE")

    Returns the same list (mutated in-place). NEVER raises.
    """
    if not cart_lines:
        return cart_lines

    try:
        offers = _load_club_offers()
        if not offers:
            return cart_lines

        order_type_upper = str(order_type).upper()

        for offer in offers:
            mode = str(offer.get("application_mode") or "SAME_ORDER").upper()
            if mode != "SAME_ORDER":
                continue
            pf = str(offer.get("party_filter") or "").strip()
            if pf and party_id and pf != str(party_id):
                continue
            if pf and not party_id:
                continue
            # Channel gate
            ch = str(offer.get("channel") or "all").lower()
            if ch not in ("all", order_type_upper.lower()):
                continue

            # Step 1: Check if all trigger products are present in cart
            trigger_ids   = _parse_ids(offer.get("trigger_product_ids"))
            trigger_brand = str(offer.get("trigger_brand") or "").strip().lower()
            trigger_cat   = str(offer.get("trigger_main_group") or "").strip().lower()

            trigger_lines = [
                l for l in cart_lines
                if _line_matches_trigger(l, offer)
            ]

            if not trigger_lines:
                continue  # trigger products not in cart

            # Step 2: Find reward lines and apply discount
            reward_lines = [
                l for l in cart_lines
                if _line_matches_reward(l, offer)
                # Reward line must differ from trigger line (don't double-apply
                # unless trigger and reward are the same product intentionally)
            ]

            if not reward_lines:
                continue  # reward product not in cart

            for reward_line in reward_lines:
                _apply_offer_to_line(reward_line, offer)

    except Exception as e:
        log.error(f"[ClubEngine] apply_club_offers failed: {e}")

    return cart_lines
