"""
modules/pricing/discount_engine.py
====================================
Wiring layer — connects DB rules to the original DiscountEngine.

VERSION: Phase2B-final  (2026-04-13)
STACKING: ✅ base + stackable combination implemented in _apply_to_line
          engine.calculate() is NOT used — replaced with direct calls to
          filter_rules / pick_best_rule / compute_discount from engine.py

THIS IS THE ONLY FILE TO EDIT for discount wiring.
The original engine.py / discount_rule.py are FROZEN — do not modify them.

What this file does:
  1. Loads rules from public.discount_rules table
  2. Converts DB rows -> DiscountRule objects (via DiscountRule.from_dict)
  3. Builds LineItem from cart line dict (enriching brand_group, product_id, etc.)
  4. Calls original engine.filter_rules() + pick_best_rule()
  5. Stamps line dict with discount_percent / discount_amount / discount_rule

Key mapping:
  DB column  discount_rules.brand         -> conditions.brand_groups = [brand]
  DB column  discount_rules.party_id      -> conditions.party_ids = [party_id]
  DB column  discount_rules.main_group    -> stored in conditions (extra field, not used by engine)
  cart field line["brand"]                -> LineItem.brand_group
  cart field line["product_id"]           -> LineItem.product_id
  cart field line["party_id"]             -> LineItem.party_id

Zero-risk guarantee:
  Every call is wrapped in try/except.
  If this module crashes, lines save with product-level discount_percent fallback.
  NEVER blocks an order save.
"""

from __future__ import annotations
import logging
from typing import Optional, List

log = logging.getLogger(__name__)

# ── Phase 2D: Minimum margin guard ──────────────────────────────────────────
# Discount is capped when net price would fall below cost × (1 + _MIN_MARGIN_PCT/100).
# Soft protection only — order save is never blocked.
# Matches engine.py DEFAULT_MARGIN_HARD_STOP_PCT = 5.
# Change here to override without touching the frozen engine.
_MIN_MARGIN_PCT: float = 5.0

# ── Rule cache ───────────────────────────────────────────────────────────────
_ENGINE_CACHE: dict = {}          # channel -> DiscountEngine instance
_ENGINE_CACHE_TS: float = 0.0
_CACHE_TTL: float = 60.0          # seconds


def _load_engine(channel: str = "wholesale"):
    """
    Load DB rules and return a DiscountEngine instance, cached for 60s.
    Converts DB rows to DiscountRule objects using DiscountRule.from_dict().
    """
    import time
    global _ENGINE_CACHE, _ENGINE_CACHE_TS
    now = time.time()

    if channel in _ENGINE_CACHE and (now - _ENGINE_CACHE_TS) < _CACHE_TTL:
        return _ENGINE_CACHE[channel]

    from modules.pricing.engine import DiscountEngine
    from modules.pricing.discount_rule import DiscountRule

    try:
        from modules.sql_adapter import run_query

        # Try full query first (all migrations applied)
        try:
            rows = run_query("""
                SELECT
                    id::text                        AS id,
                    name,
                    COALESCE(type, 'product')       AS type,
                    COALESCE(priority, 4)           AS priority,
                    COALESCE(value_type, 'percent') AS value_type,
                    COALESCE(value, 0)              AS value,
                    special_price,
                    COALESCE(bogo_buy, 0)           AS bogo_buy,
                    COALESCE(bogo_get, 0)           AS bogo_get,
                    slab_config, conditions,
                    conditions_dsl,
                    COALESCE(gst_rate, 12)          AS gst_rate,
                    COALESCE(active, TRUE)          AS active,
                    COALESCE(channel, conditions->>'channel', 'all') AS channel,
                    COALESCE(stackable, FALSE)      AS stackable,
                    COALESCE(conflict_strategy, 'best_price') AS conflict_strategy
                FROM discount_rules
                WHERE COALESCE(active, TRUE) = TRUE
                ORDER BY priority ASC
            """) or []
        except Exception:
            # Fallback: migration 002/003 not yet applied — use baseline columns only
            rows = run_query("""
                SELECT
                    id::text                        AS id,
                    name,
                    COALESCE(type, 'product')       AS type,
                    COALESCE(priority, 4)           AS priority,
                    COALESCE(value_type, 'percent') AS value_type,
                    COALESCE(value, 0)              AS value,
                    special_price,
                    COALESCE(bogo_buy, 0)           AS bogo_buy,
                    COALESCE(bogo_get, 0)           AS bogo_get,
                    slab_config, conditions,
                    NULL                            AS conditions_dsl,
                    COALESCE(gst_rate, 12)          AS gst_rate,
                    COALESCE(active, TRUE)          AS active,
                    COALESCE(conditions->>'channel', 'all') AS channel,
                    FALSE                           AS stackable,
                    'best_price'                    AS conflict_strategy
                FROM discount_rules
                WHERE COALESCE(active, TRUE) = TRUE
                ORDER BY priority ASC
            """) or []

    except Exception as e:
        log.warning(f"[DiscountEngine] DB load failed: {e}")
        rows = []

    rules: List[DiscountRule] = []
    for row in rows:
        try:
            # ── Normalise row into the format DiscountRule.from_dict expects ──
            import json

            # conditions JSONB from DB
            cond = row.get("conditions") or {}
            if isinstance(cond, str):
                try:
                    cond = json.loads(cond)
                except Exception:
                    cond = {}

            # conditions JSONB already has party_ids, brand_groups, product_ids
            # from our save functions — no flat column mapping needed.
            # channel normalisation
            ch = str(row.get("channel") or "all").lower()
            cond["channel"] = ch if ch in ("wholesale","retail","online","all") else "all"

            rule_dict = {
                "id":                row["id"],
                "name":              row.get("name", ""),
                "type":              row.get("type", "product"),
                "priority":          int(row.get("priority") or 4),
                "value_type":        row.get("value_type", "percent"),
                "value":             row.get("value"),
                "special_price":     row.get("special_price"),
                "bogo_buy":          row.get("bogo_buy"),
                "bogo_get":          row.get("bogo_get"),
                "slab_config":       row.get("slab_config"),
                "gst_rate":          row.get("gst_rate", 12),
                "active":            bool(row.get("active", True)),
                "conditions":        cond,
                "conditions_dsl":    row.get("conditions_dsl"),
                # ── Phase 2B: conflict strategy fields ──────────────────────
                # stackable=True  → rule joins pick_stackable_rules() path
                #   e.g. offer_slab, offer_bogo, offer_promo set stackable=TRUE
                #   in admin UI — engine then stacks them on top of best base rule
                # stackable=False → best-wins only (default for party/product/brand)
                "stackable":         bool(row.get("stackable", False)),
                "conflict_strategy": str(row.get("conflict_strategy") or "best_price"),
            }

            rule = DiscountRule.from_dict(rule_dict)
            rules.append(rule)

        except Exception as re:
            log.debug(f"[DiscountEngine] Skipping rule {row.get('id','?')}: {re}")

    engine = DiscountEngine(rules)
    _ENGINE_CACHE[channel] = engine
    _ENGINE_CACHE_TS = now
    log.info(f"[DiscountEngine] Loaded {len(rules)} rules for channel={channel}")
    return engine


# Party type cache — avoids DB hit on every line in same order
_PARTY_TYPE_CACHE: dict = {}

def _get_party_type(party_id: str) -> tuple:
    """
    Fetch party_type and price_tier for a party UUID.
    Returns (party_type, price_tier) tuple. Cached in-process per worker.
    Phase 3B: also fetches price_tier so tier-based discount rules fire.
    """
    global _PARTY_TYPE_CACHE
    if party_id in _PARTY_TYPE_CACHE:
        cached = _PARTY_TYPE_CACHE[party_id]
        # Handle old cache entries that stored just a string
        if isinstance(cached, tuple):
            return cached
        return (cached, "")
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT COALESCE(party_type,\'\') AS pt, "
            "COALESCE(price_tier,\'standard\') AS tier "
            "FROM parties WHERE id=%s::uuid LIMIT 1",
            (party_id,)
        ) or []
        pt   = rows[0]["pt"]   if rows else ""
        tier = rows[0]["tier"] if rows else "standard"
        _PARTY_TYPE_CACHE[party_id] = (pt, tier)
        return (pt, tier)
    except Exception:
        return ("", "")


def _map_party_type_to_tags(party_type: str, price_tier: str = "") -> list:
    """
    Convert DB party_type + price_tier to tag list for DiscountEngine filter_rules().

    Wholesale types → always get "wholesale" tag so rules with
    party_tags=["wholesale"] fire for all of them automatically.

    Optician / Doctor / Distributor / Dealer / Hospital / Clinic /
    Trader / Stockist / Agent → ["wholesale", "<party_type>"]

    Retailer / Customer → ["retail", "<party_type>"]

    Phase 3B: price_tier appended as extra tag (vip/gold/silver etc).
    """
    tags = []
    pt   = party_type.strip().lower() if party_type else ""

    WHOLESALE_TYPES = {
        "doctor", "distributor", "dealer", "optician",
        "hospital", "clinic", "trader", "stockist",
        "agent", "wholesale",
    }
    RETAIL_TYPES = {"retailer", "customer", "retail"}

    if pt in WHOLESALE_TYPES:
        tags.append("wholesale")
    elif pt in RETAIL_TYPES:
        tags.append("retail")

    # Also append raw type so specific rules can target e.g. party_tags=["optician"]
    if pt and pt not in tags:
        tags.append(pt)

    # Phase 3B: tier tag
    tier = price_tier.strip().lower() if price_tier else ""
    if tier and tier not in tags:
        tags.append(tier)
    return tags




# ── Phase 3C: Day + Time schedule check ──────────────────────────────────────
def _passes_schedule(rule) -> bool:
    """
    Check day-of-week and time-window conditions stored in rule.conditions JSONB.
    The frozen engine already handles valid_from/valid_to via RuleConditions.
    This handles the additional days/time_from/time_to fields.

    conditions JSONB example:
      {"days": ["SAT","SUN"], "time_from": "18:00", "time_to": "22:00"}

    Returns True (allow) if conditions pass or are absent.
    Returns False (block) only when conditions are set and not met.
    Fail-safe: any exception → True (never block a rule due to parsing error).
    """
    try:
        from datetime import datetime
        import json

        cond = getattr(rule, "conditions", None)
        if cond is None:
            return True

        # RuleConditions object — get raw dict via to_dict()
        if hasattr(cond, "to_dict"):
            cond_d = cond.to_dict()
        elif isinstance(cond, dict):
            cond_d = cond
        else:
            return True

        now = datetime.now()

        # Day-of-week check (MON / TUE / WED / THU / FRI / SAT / SUN)
        days = cond_d.get("days")
        if days and isinstance(days, list) and days:
            today = now.strftime("%a").upper()[:3]   # e.g. "SAT"
            if today not in [d.upper()[:3] for d in days]:
                return False

        # Time window check (HH:MM format)
        time_from = cond_d.get("time_from")
        time_to   = cond_d.get("time_to")
        if time_from and time_to:
            t_from = datetime.strptime(str(time_from), "%H:%M").time()
            t_to   = datetime.strptime(str(time_to),   "%H:%M").time()
            now_t  = now.time()
            # Handle overnight windows e.g. 22:00–02:00
            if t_from <= t_to:
                if not (t_from <= now_t <= t_to):
                    return False
            else:
                if not (now_t >= t_from or now_t <= t_to):
                    return False

        return True

    except Exception:
        return True   # fail-safe: never block on parsing error


# ── Phase 3B: Pricing tier discount (max with base rule) ─────────────────────
_TIER_CACHE: dict = {}
_TIER_CACHE_TS: float = 0.0
_TIER_CACHE_TTL: float = 300.0   # 5 minutes — tier table changes rarely


def _get_party_tier_discount(party_id: str) -> tuple:
    """
    Fetch tier-level discount % and allow_stacking flag for a party.
    Returns (discount_percent: float, allow_stacking: bool).
    Returns (0.0, True) when no tier is set or tier has no discount.

    pricing_tiers table:
      tier_name TEXT UNIQUE, discount_percent FLOAT, allow_stacking BOOLEAN DEFAULT TRUE

    Formula: Final = MAX(base_rule_disc, tier_disc) + stack_rules (if allow_stacking)
    """
    import time
    global _TIER_CACHE, _TIER_CACHE_TS

    now = time.time()
    if party_id in _TIER_CACHE and (now - _TIER_CACHE_TS) < _TIER_CACHE_TTL:
        return _TIER_CACHE[party_id]

    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COALESCE(pt.discount_percent, 0) AS disc_pct,
                COALESCE(pt.allow_stacking, TRUE) AS allow_stack
            FROM parties p
            LEFT JOIN pricing_tiers pt
                   ON pt.tier_name = COALESCE(p.price_tier, 'standard')
            WHERE p.id = %s::uuid
            LIMIT 1
        """, (party_id,)) or []

        result = (
            float(rows[0].get("disc_pct") or 0),
            bool(rows[0].get("allow_stack", True))
        ) if rows else (0.0, True)

        _TIER_CACHE[party_id] = result
        _TIER_CACHE_TS        = now
        return result
    except Exception:
        return (0.0, True)


def _invalidate_tier_cache() -> None:
    global _TIER_CACHE, _TIER_CACHE_TS
    _TIER_CACHE    = {}
    _TIER_CACHE_TS = 0.0


def _build_line_item(line: dict, party_id: str, order_type: str):
    """
    Build a LineItem from a cart line dict for the DiscountEngine.

    Fields used for rule matching:
      brand       -> LineItem.brand_group  (products.brand)
      product_id  -> LineItem.product_id
      party_id    -> LineItem.party_id
      party_tags  -> LineItem.party_tags   (derived from party_type)

    party_tags mapping (so tag-based rules fire — e.g. "Wholesale Standard 10%"):
      WHOLESALE / DEALER / DISTRIBUTOR -> ["wholesale"]
      RETAILER                         -> ["retail"]
      + "vip" / "gold" appended if in party_type
    If no party found, falls back from order_type.
    """
    from modules.pricing.discount_rule import LineItem, SalesChannel
    from decimal import Decimal

    unit_price = float(line.get("unit_price") or 0)
    qty        = int(line.get("billing_qty") or line.get("quantity") or 1)

    channel_map = {
        "RETAIL":    SalesChannel.RETAIL,
        "WHOLESALE": SalesChannel.WHOLESALE,
        "ONLINE":    SalesChannel.ONLINE,
    }
    channel = channel_map.get(str(order_type).upper(), SalesChannel.WHOLESALE)

    brand_group = (
        str(line.get("brand") or "").strip()
        or str(line.get("brand_group") or "").strip()
        or None
    )
    product_id  = str(line.get("product_id") or "").strip() or None
    product_cat = str(line.get("main_group") or line.get("category") or "").strip() or None

    pid = (
        str(party_id or "").strip()
        or str(line.get("party_id") or "").strip()
        or None
    )

    # Build party_tags — engine matches these against conditions.party_tags
    party_tags = list(line.get("party_tags") or [])
    if not party_tags:
        if pid:
            _pt, _tier = _get_party_type(pid)
            party_tags = _map_party_type_to_tags(_pt, _tier)
        if not party_tags:
            # Fallback: derive from order_type when no party found
            ot = str(order_type).upper()
            if ot == "WHOLESALE":
                party_tags = ["wholesale"]
            elif ot == "RETAIL":
                party_tags = ["retail"]

    _raw_cost = None
    try:
        if line.get("purchase_rate") and float(line["purchase_rate"]) > 0:
            _raw_cost = Decimal(str(line["purchase_rate"]))
        elif line.get("cost_price") and float(line["cost_price"]) > 0:
            _raw_cost = Decimal(str(line["cost_price"]))
        _box_size = int(float(line.get("box_size") or 1))
        # Cart/order unit_price is always per PCS. Inventory purchase_rate for
        # contact-lens boxes is often stored per BOX, so normalize it to PCS
        # before margin calculation. Otherwise a ₹1667 box cost is compared
        # against a ₹297 pcs sell price and creates false -500% hard stops.
        if _raw_cost is not None and _box_size > 1 and _raw_cost > Decimal(str(unit_price)) * Decimal("1.5"):
            _raw_cost = (_raw_cost / Decimal(_box_size)).quantize(Decimal("0.01"))
    except Exception:
        _raw_cost = None

    return LineItem(
        base_price  = Decimal(str(unit_price)),
        quantity    = qty,
        product_id  = product_id,
        product_cat = product_cat,
        party_id    = pid,
        party_tags  = party_tags,
        brand_group = brand_group,
        channel     = channel,
        promo_code  = str(line.get("promo_code") or "").strip() or None,
        # Phase 2D: pass cost_price so engine can compute margin status.
        # purchase_rate is the cost column stamped on cart lines from stock table.
        # Falls back to cost_price if already normalised. None = margin skipped.
        cost_price  = _raw_cost,
    )



def _apply_to_line(line: dict, engine, party_id: str, order_type: str) -> None:
    """
    Apply discount rules to one cart line. Mutates in place.

    Phase 2B stacking logic — replaces single engine.calculate() call.

    The frozen engine.calculate() / pick_stackable_rules() cannot combine
    a non-stackable base rule (e.g. Party 10%) with stackable offers
    (e.g. Slab 5%) — it picks the BETTER group, never both.

    This function implements true base + stack combination:
      1. filter_rules()    — all applicable rules for this line/channel
      2. Special price P1  — hard override, stops everything else
      3. pick_best_rule()  — best NON-stackable base (party/product/brand)
      4. stackable rules   — apply each on net-after-base (compounding)
      5. combine           — base_disc + stacked_disc = final discount
      6. margin check      — cap if net < cost × (1 + _MIN_MARGIN_PCT%)
      7. stamp all fields  — discount_percent, discount_amount, discount_rule,
                             applied_rule_ids, discount_breakdown, scheme_info,
                             margin_status, margin_pct, margin_blocked

    All frozen engine functions (filter_rules, compute_discount, pick_best_rule,
    compute_margin) are used as-is. Only the combination layer is ours.
    """
    unit_price = float(line.get("unit_price") or 0)
    qty        = int(line.get("billing_qty") or line.get("quantity") or 1)

    if unit_price <= 0:
        line.setdefault("discount_percent",   0.0)
        line.setdefault("discount_amount",    0.0)
        line.setdefault("discount_rule",      "")
        line.setdefault("applied_rule_ids",   "")
        line.setdefault("discount_breakdown", [])
        line.setdefault("scheme_info",        {})
        line.setdefault("margin_status",      "ok")
        line.setdefault("margin_pct",         0.0)
        line.setdefault("margin_blocked",     False)
        return

    try:
        from modules.pricing.engine import (
            filter_rules, compute_discount, pick_best_rule, compute_margin,
        )
        from decimal import Decimal

        item  = _build_line_item(line, party_id, order_type)
        gross = item.gross

        # Step 1: Filter all applicable rules for this line + channel
        channel_rules = engine._get_rules_for_channel(item.channel)
        applicable    = filter_rules(channel_rules, item)

        # Phase 3C: Remove rules whose day-of-week / time window conditions fail
        applicable = [r for r in applicable if _passes_schedule(r)]

        if not applicable:
            _stamp_line(line, unit_price, qty, item, 0.0, 0.0, "", [], [], None, 0.0)
            return

        # Step 2: Special price (priority=1) — hard override, no stacking
        specials = [r for r in applicable if r.priority == 1]
        if specials:
            rule           = specials[0]
            disc_amt_d, _  = compute_discount(rule, item)
            disc_amt       = float(disc_amt_d)
            disc_pct       = round(disc_amt / float(gross) * 100, 4) if float(gross) > 0 else 0.0
            _stamp_line(line, unit_price, qty, item, disc_pct, disc_amt,
                        rule.name, [rule], [], None, 0.0)
            return

        # Step 3: Split base rules (non-stackable) vs stackable offers
        base_rules      = [r for r in applicable if not r.stackable]
        stackable_rules = [r for r in applicable if r.stackable and r.priority != 1]

        # Step 4: Pick the single best base rule
        base_rule = None
        base_disc = Decimal("0")
        if base_rules:
            base_rule, base_disc, _ = pick_best_rule(base_rules, item)

        # Phase 3B: Tier discount — MAX(base_rule, tier) as the effective base
        # allow_stacking=False on tier means stackable offers also blocked for this party
        _tier_disc_pct  = 0.0
        _tier_stacking  = True
        _pid_for_tier   = str(party_id or "").strip() or str(item.party_id or "").strip()
        if _pid_for_tier:
            _tier_disc_pct, _tier_stacking = _get_party_tier_discount(_pid_for_tier)

        # Tier name for traceability (stamped into applied_rule_ids + breakdown)
        _tier_name = ""
        if _pid_for_tier and _tier_disc_pct > 0:
            try:
                from modules.sql_adapter import run_query as _rq_t
                _trows = _rq_t(
                    "SELECT COALESCE(price_tier, 'standard') AS tier "
                    "FROM parties WHERE id=%s::uuid LIMIT 1",
                    (_pid_for_tier,)
                ) or []
                _tier_name = str(_trows[0].get("tier") or "") if _trows else ""
            except Exception:
                _tier_name = "tier"

        if _tier_disc_pct > 0:
            tier_disc_amt = (gross * Decimal(str(_tier_disc_pct)) / 100).quantize(Decimal("0.01"))
            if tier_disc_amt > base_disc:
                # Tier beats the rule — use tier as base
                base_disc = tier_disc_amt
                base_rule = None   # no DiscountRule object for tier
                log.debug(
                    f"[TierDiscount] Tier {_tier_disc_pct}% ({_tier_name}) > rule "
                    f"for party {_pid_for_tier[:8]}"
                )

        # Step 5: Apply each stackable rule on net-after-base (compounding)
        # Respect tier allow_stacking flag
        fired_stack  = []   # list of (rule, disc_amount_float)
        stacked_disc = Decimal("0")
        current_net  = gross - base_disc

        if stackable_rules and current_net > Decimal("0") and _tier_stacking:
            for rule in sorted(stackable_rules, key=lambda r: (r.priority, r.name)):
                # Percent rules compound on current net; others use gross qty logic
                if rule.value_type.value == "percent" and rule.value is not None:
                    s_disc = (current_net * rule.value / 100).quantize(Decimal("0.01"))
                else:
                    s_disc, _ = compute_discount(rule, item)

                if s_disc > Decimal("0"):
                    stacked_disc += s_disc
                    current_net  -= s_disc
                    fired_stack.append((rule, float(s_disc)))

        # Step 6: Combine and clamp
        total_disc_amt = float(base_disc + stacked_disc)
        total_disc_pct = round(total_disc_amt / float(gross) * 100, 4) if float(gross) > 0 else 0.0
        total_disc_amt = max(0.0, min(total_disc_amt, float(gross)))
        total_disc_pct = max(0.0, min(total_disc_pct, 100.0))

        # Build fired list — include tier as a pseudo-entry when it replaced base_rule
        all_fired = ([base_rule] if base_rule else []) + [r for r, _ in fired_stack]
        rule_name_parts = []
        if base_rule:
            rule_name_parts.append(base_rule.name)
        elif _tier_name and float(base_disc) > 0:
            rule_name_parts.append(f"Tier:{_tier_name.upper()}")
        rule_name_parts += [r.name for r, _ in fired_stack]
        rule_name = " + ".join(rule_name_parts) if rule_name_parts else ""

        _tier_label = (f"Tier:{_tier_name.upper()}"
                       if (_tier_name and not base_rule and float(base_disc) > 0)
                       else "")
        _stamp_line(line, unit_price, qty, item,
                    total_disc_pct, total_disc_amt,
                    rule_name, all_fired, fired_stack,
                    base_rule, float(base_disc),
                    tier_label=_tier_label,
                    tier_disc_pct=_tier_disc_pct)

    except Exception as e:
        log.debug(f"[DiscountEngine] Line skipped: {e}")
        fallback_pct = float(line.get("discount_percent") or 0)
        line["discount_percent"] = fallback_pct
        line["discount_amount"]  = round(unit_price * qty * fallback_pct / 100, 2)
        line["discount_rule"]    = "product_default" if fallback_pct else ""
        line["applied_rule_ids"] = ""
        line.setdefault("discount_breakdown", [])
        line.setdefault("scheme_info",        {})
        line.setdefault("margin_status",      "ok")
        line.setdefault("margin_pct",         0.0)
        line.setdefault("margin_blocked",     False)


def _stamp_line(
    line: dict,
    unit_price: float,
    qty: int,
    item,
    disc_pct: float,
    disc_amt: float,
    rule_name: str,
    all_fired: list,
    fired_stack: list,
    base_rule,
    base_disc: float,
    tier_label: str = "",
    tier_disc_pct: float = 0.0,
) -> None:
    """
    Stamp all discount, trace, breakdown, and margin fields onto the line dict.
    Called from _apply_to_line after combination logic is complete.
    """
    from decimal import Decimal

    gross = unit_price * qty

    # ── Core pricing fields ──────────────────────────────────────────────────
    line["discount_percent"] = round(disc_pct, 4)
    line["discount_amount"]  = round(disc_amt, 2)
    line["discount_rule"]    = rule_name

    # ── Phase 2A: Rule trace ─────────────────────────────────────────────────
    # applied_rule_ids: real rule UUIDs + tier pseudo-id for traceability
    rule_id_parts = [str(r.id) for r in all_fired if r]
    if tier_label:
        rule_id_parts.insert(0, tier_label)   # e.g. "Tier:VIP"
    line["applied_rule_ids"] = ",".join(rule_id_parts)

    # ── Phase 2C: Breakdown + scheme_info ───────────────────────────────────
    try:
        breakdown   = []
        scheme_info = {}
        _ICON = {
            "party":       "🏷️",  "brand_group": "🔖",
            "product":     "📦",  "special":     "⭐",
            "offer_slab":  "📊",  "offer_bogo":  "🎁",
            "promo_code":  "🎟️", "coating":     "✨",
        }

        # Tier entry — shown when tier overrides base rule
        if tier_label and tier_disc_pct > 0 and not base_rule:
            gross_val = unit_price * qty
            tier_pct_display = round(float(base_disc) / gross_val * 100, 2) if gross_val > 0 else 0.0
            breakdown.append({
                "type":  "tier",
                "label": f"Pricing Tier ({tier_label.replace('Tier:','')})",
                "icon":  "🎯",
                "value": tier_pct_display,
                "name":  tier_label,
            })

        # Base rule entry
        if base_rule and base_disc > 0:
            rtype = base_rule.type.value
            lbl   = (base_rule.display_label or {
                "party": "Party Discount", "brand_group": "Brand Offer",
                "product": "Product Discount", "special": "Special Price",
                "coating": "Coating Upgrade",
            }.get(rtype, base_rule.name))
            rule_pct = round(float(base_rule.value or 0), 2) if base_rule.value else (
                round(base_disc / gross * 100, 2) if gross else 0.0
            )
            breakdown.append({
                "type": rtype, "label": lbl,
                "icon": base_rule.icon_emoji or _ICON.get(rtype, "💰"),
                "value": rule_pct, "name": base_rule.name,
            })

        # Stackable rule entries
        for rule, s_disc in fired_stack:
            rtype = rule.type.value
            if rule.display_label:
                lbl = rule.display_label
            elif rtype == "offer_slab":
                mp, mm = 0.0, 0
                for slab in (rule.slab_config or []):
                    if slab.matches(qty):
                        mp, mm = float(slab.discount_pct), slab.min_qty
                        break
                lbl = f"Slab: Buy {mm}+ → {mp:.0f}% off"
            elif rtype == "offer_bogo":
                lbl = f"Buy {rule.bogo_buy or 0} Get {rule.bogo_get or 0} Free"
            elif rtype == "promo_code":
                lbl = f"Coupon ({(rule.conditions.promo_code or '').upper()})"
            else:
                lbl = rule.name

            rule_pct = round(s_disc / gross * 100, 2) if gross > 0 else 0.0
            breakdown.append({
                "type": rtype, "label": lbl,
                "icon": rule.icon_emoji or _ICON.get(rtype, "💰"),
                "value": rule_pct, "name": rule.name,
            })
            if rtype == "offer_bogo" and rule.bogo_buy:
                scheme_info = {
                    "type":        "bogo",
                    "description": f"Buy {rule.bogo_buy} Get {rule.bogo_get} Free",
                    "free_qty":    (qty // rule.bogo_buy) * (rule.bogo_get or 0),
                    "bogo_buy":    rule.bogo_buy,
                    "bogo_get":    rule.bogo_get or 0,
                }

        line["discount_breakdown"] = breakdown
        line["scheme_info"]        = scheme_info

    except Exception as _e:
        line.setdefault("discount_breakdown", [])
        line.setdefault("scheme_info",        {})
        log.debug(f"[DiscountEngine] breakdown failed: {_e}")

    # ── Phase 2D: Margin protection ──────────────────────────────────────────
    try:
        from modules.pricing.engine import compute_margin
        net_d              = Decimal(str(round(gross - disc_amt, 2)))
        _, m_pct, m_status = compute_margin(net_d, item.cost_gross)
        margin_pct         = float(m_pct or 0)
        margin_blocked     = False

        if m_status == "hard_stop":
            cost_pr = float(line.get("purchase_rate") or line.get("cost_price") or 0)
            try:
                _bs_m = int(float(line.get("box_size") or 1))
                if _bs_m > 1 and cost_pr > float(line.get("unit_price") or 0) * 1.5:
                    cost_pr = round(cost_pr / _bs_m, 2)
            except Exception:
                pass
            if cost_pr > 0 and gross > 0:
                min_net      = cost_pr * qty * (1 + _MIN_MARGIN_PCT / 100)
                max_disc     = max(0.0, gross - min_net)
                max_pct      = round(max_disc / gross * 100, 4)
                if disc_pct > max_pct:
                    disc_pct = max_pct
                    disc_amt = round(gross * disc_pct / 100, 2)
                    line["discount_percent"] = disc_pct
                    line["discount_amount"]  = disc_amt
                    margin_blocked = True
                    log.info(
                        f"[DiscountEngine] Margin cap: "
                        f"{line.get('product_name','?')} → {disc_pct:.2f}% max"
                    )

        line["margin_status"]  = m_status
        line["margin_pct"]     = round(margin_pct, 2)
        line["margin_blocked"] = margin_blocked

    except Exception as _me:
        line.setdefault("margin_status",  "ok")
        line.setdefault("margin_pct",     0.0)
        line.setdefault("margin_blocked", False)

    log.debug(
        f"[DiscountEngine] {line.get('product_name','?')} "
        f"→ {line['discount_percent']:.2f}% = ₹{line['discount_amount']:.2f} "
        f"rules=[{rule_name}] margin={line.get('margin_status','ok')}"
    )

def apply_discounts(
    lines: list,
    party_id: str = "",
    order_type: str = "wholesale",
) -> list:
    """
    PUBLIC API — Apply discount rules to a list of cart lines.

    Mutates each line in-place, adding:
        discount_percent  (float)
        discount_amount   (float)
        discount_rule     (str — rule name for audit/display)

    Returns the same list. NEVER raises. Safe to call before any save.

    Usage:
        from modules.pricing.discount_engine import apply_discounts
        lines = apply_discounts(lines, party_id=party_id, order_type="WHOLESALE")

    For rules to fire, each line dict MUST contain:
        brand       — from products.brand (e.g. "Alcon")
        product_id  — UUID string
        party_id    — UUID string (or pass via party_id argument)
    """
    if not lines:
        return lines

    channel = "retail" if str(order_type).upper() == "RETAIL" else "wholesale"

    try:
        engine = _load_engine(channel=channel)
        for line in lines:
            _apply_to_line(line, engine, party_id, order_type)
            try:
                from modules.pricing.supplier_scheme_engine import apply_customer_scheme_to_line
                apply_customer_scheme_to_line(line, party_id=party_id, order_type=order_type)
            except Exception:
                pass

    except Exception as e:
        log.error(f"[DiscountEngine] apply_discounts failed: {e} — product fallback")
        for line in lines:
            pct = float(line.get("discount_percent") or 0)
            up  = float(line.get("unit_price") or 0)
            qty = int(line.get("billing_qty") or line.get("quantity") or 1)
            line.setdefault("discount_percent", pct)
            line.setdefault("discount_amount",  round(up * qty * pct / 100, 2))
            line.setdefault("discount_rule",    "product_default" if pct else "")
            line.setdefault("applied_rule_ids", "")
            line.setdefault("discount_breakdown", [])
            line.setdefault("scheme_info",        {})
            line.setdefault("margin_status",      "ok")
            line.setdefault("margin_pct",         0.0)
            line.setdefault("margin_blocked",     False)

    return lines


def invalidate_rule_cache() -> None:
    """
    Force reload rules and rebuild DiscountEngine on next call.
    Call this after adding/editing/deleting a rule in admin UI.
    """
    global _ENGINE_CACHE, _ENGINE_CACHE_TS
    _ENGINE_CACHE = {}
    _ENGINE_CACHE_TS = 0.0
    log.info("[DiscountEngine] Cache invalidated — rules will reload on next order")
