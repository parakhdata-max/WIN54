"""
optical_discount_engine/core/engine.py — v2.1 (polished)

POLISH APPLIED IN THIS VERSION:
  P1. Rule priority map clarified — added PARTY_CONTRACT tier between PARTY and BRAND_GROUP
      Priority ladder: Special(1) > Party Contract(2) > Party/BrandGroup(3) > Product/Coating/Promo(4) > Offers(5)
      Existing rules unaffected — priority numbers extended, not shifted
  P2. Promo code normalization — .upper().strip() applied at SOURCE inside _normalize_promo()
      Both item code AND rule code are normalized before storage and before comparison
  P3. Margin warning with tiering — three levels: OK / SOFT_WARNING / HARD_STOP
      DiscountEngine accepts soft_warning_pct and hard_stop_pct thresholds
      hard_stop raises MarginHardStopError — caller decides what to do (block or warn)
  P4. Stackable rule engine — pick_best_rule() now handles two modes:
      - stackable=False (default): best-wins, one rule fires
      - stackable=True:  rules accumulate sequentially on net price
      Mixed groups handled: stackable rules stack among themselves,
      non-stackable best-wins block applies first if it yields a higher discount
  P5. Performance: lightweight rule cache keyed by (channel, type) — O(1) lookup
      Compiled at DiscountEngine.__init__, transparent to callers
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple, Dict

try:
    from .discount_rule import (
        DiscountRule, DiscountResult, LineItem,
        RuleConditions, RuleType, SalesChannel, SlabTier, ValueType,
    )
except ImportError:
    from pricing.discount_rule import (
        DiscountRule, DiscountResult, LineItem,
        RuleConditions, RuleType, SalesChannel, SlabTier, ValueType,
    )


TWO_PLACES = Decimal("0.01")

# ── P3: Margin tier defaults ────────────────────────────────────────────────
DEFAULT_MARGIN_HARD_STOP_PCT  = Decimal("5")    # Block / raise if margin < 5%
DEFAULT_MARGIN_SOFT_WARN_PCT  = Decimal("15")   # Yellow warning if margin < 15%
# Margin status labels
MARGIN_OK           = "ok"
MARGIN_SOFT_WARNING = "soft_warning"
MARGIN_HARD_STOP    = "hard_stop"


# ── P2: Promo normalization helper ──────────────────────────────────────────

def _normalize_promo(code: Optional[str]) -> Optional[str]:
    """
    Normalize a promo code to its canonical form.
    Always .strip().upper() — applied to BOTH item code and rule code.
    Returns None for empty/whitespace strings.
    """
    if not code:
        return None
    normalized = code.strip().upper()
    return normalized if normalized else None


# ── P3: MarginHardStopError ─────────────────────────────────────────────────

class MarginHardStopError(Exception):
    """
    Raised when applying a discount would push margin below hard_stop_pct.
    Caller (billing system) decides: block the sale, require manager approval, etc.
    """
    def __init__(self, rule_name: str, margin_pct: Decimal, threshold: Decimal):
        self.rule_name  = rule_name
        self.margin_pct = margin_pct
        self.threshold  = threshold
        super().__init__(
            f"Hard stop: Rule '{rule_name}' pushes margin to {margin_pct:.1f}% "
            f"(threshold: {threshold:.1f}%)"
        )


# ────────────────────────────────────────────────────────────────────────────
# STEP 1 — FILTER
# ────────────────────────────────────────────────────────────────────────────

def filter_rules(rules: List[DiscountRule], item: LineItem) -> List[DiscountRule]:
    """
    Return all active rules applicable to the given line item.

    Check order:
      active → date → qty range → min amount
      → party blacklist → party whitelist
      → party tags/ids → product cat/ids
      → brand groups → channel → promo code (P2: normalized)
    """
    applicable = []
    today = date.today()

    # P2: normalize item promo code once, not on every rule iteration
    item_promo_normalized = _normalize_promo(item.promo_code)

    for rule in rules:
        if not rule.active:
            continue

        c: RuleConditions = rule.conditions

        # ── Date validity ────────────────────────────
        if c.valid_from and today < c.valid_from:
            continue
        if c.valid_to and today > c.valid_to:
            continue

        # ── Quantity range ───────────────────────────
        if c.min_qty is not None and item.quantity < c.min_qty:
            continue
        if c.max_qty is not None and item.quantity > c.max_qty:
            continue

        # ── Minimum amount ───────────────────────────
        if c.min_amount is not None and item.gross < c.min_amount:
            continue

        # ── Party blacklist (hard exclude, checked first) ──
        if c.party_blacklist and item.party_id and item.party_id in c.party_blacklist:
            continue

        # ── Party whitelist (if set, item must be in it) ───
        if c.party_whitelist:
            if not item.party_id or item.party_id not in c.party_whitelist:
                continue

        # ── Party tags / IDs ─────────────────────────
        party_required = bool(c.party_ids or c.party_tags)
        if party_required:
            party_match = False
            if c.party_ids and item.party_id in c.party_ids:
                party_match = True
            if c.party_tags and any(t in item.party_tags for t in c.party_tags):
                party_match = True
            if not party_match:
                continue

        # ── Product category / IDs ────────────────────
        product_required = bool(c.product_ids or c.product_cats)
        if product_required:
            prod_match = False
            if c.product_ids and item.product_id in c.product_ids:
                prod_match = True
            if c.product_cats and item.product_cat in c.product_cats:
                prod_match = True
            if not prod_match:
                continue

        # ── Brand group ───────────────────────────────
        if c.brand_groups:
            if not item.brand_group or item.brand_group not in c.brand_groups:
                continue

        # ── Sales channel ─────────────────────────────
        if c.channel != SalesChannel.ALL:
            if item.channel != c.channel and item.channel != SalesChannel.ALL:
                continue

        # ── Promo code  (P2: normalized comparison) ───
        if c.promo_code:
            rule_promo = _normalize_promo(c.promo_code)
            if item_promo_normalized != rule_promo:
                continue

        applicable.append(rule)

    return applicable


# ────────────────────────────────────────────────────────────────────────────
# STEP 2 — COMPUTE DISCOUNT
# ────────────────────────────────────────────────────────────────────────────

def compute_discount(rule: DiscountRule, item: LineItem) -> Tuple[Decimal, Decimal]:
    """Returns (discount_amount, effective_pct) for a rule + line item."""
    gross = item.gross

    # SLAB — must check type before value_type path
    if rule.type == RuleType.OFFER_SLAB and rule.slab_config:
        matched_pct = Decimal("0")
        for slab in rule.slab_config:
            if slab.matches(item.quantity):
                matched_pct = slab.discount_pct
                break
        return (gross * matched_pct / 100).quantize(TWO_PLACES), matched_pct

    if rule.value_type == ValueType.SPECIAL_PRICE and rule.special_price is not None:
        unit_disc = max(Decimal("0"), item.base_price - rule.special_price)
        disc_amt  = (unit_disc * item.quantity).quantize(TWO_PLACES)
        eff_pct   = (disc_amt / gross * 100).quantize(TWO_PLACES) if gross else Decimal("0")
        return disc_amt, eff_pct

    elif rule.value_type == ValueType.PERCENT and rule.value is not None:
        return (gross * rule.value / 100).quantize(TWO_PLACES), rule.value

    elif rule.value_type == ValueType.FIXED and rule.value is not None:
        disc_amt = min(rule.value, gross).quantize(TWO_PLACES)
        eff_pct  = (disc_amt / gross * 100).quantize(TWO_PLACES) if gross else Decimal("0")
        return disc_amt, eff_pct

    elif rule.value_type == ValueType.BOGO and rule.bogo_buy and rule.bogo_get:
        free_units = (item.quantity // rule.bogo_buy) * rule.bogo_get
        disc_amt   = (item.base_price * free_units).quantize(TWO_PLACES)
        eff_pct    = (disc_amt / gross * 100).quantize(TWO_PLACES) if gross else Decimal("0")
        return disc_amt, eff_pct

    return Decimal("0"), Decimal("0")


# ────────────────────────────────────────────────────────────────────────────
# STEP 3 — PICK WINNER  (P1: priority ladder + P4: stackable)
# ────────────────────────────────────────────────────────────────────────────

def pick_best_rule(
    applicable_rules: List[DiscountRule],
    item: LineItem
) -> Tuple[Optional[DiscountRule], Decimal, Decimal]:
    """
    Best-wins selection for non-stackable rules (the default path).

    P1 Priority ladder (lower number = higher priority):
        1  Special price           — hard overrides everything
        2  Party contract          — reserved for negotiated contracts
        3  Party / Brand group     — standard party or brand level
        4  Product / Coating / Promo
        5  Offers (slab, BOGO)

    Tie-breaking within same priority:
        1st  highest discount AMOUNT
        2nd  highest effective %  (handles fixed-₹ vs % comparisons)
        3rd  alphabetical name    (deterministic — no randomness ever)
    """
    if not applicable_rules:
        return None, Decimal("0"), Decimal("0")

    # Priority 1 (special price) — always hard-wins immediately
    specials = [r for r in applicable_rules if r.priority == 1]
    if specials:
        rule = specials[0]
        disc, pct = compute_discount(rule, item)
        return rule, disc, pct

    # Evaluate all non-special rules
    evaluated = [
        (rule, *compute_discount(rule, item))
        for rule in applicable_rules
        if rule.priority != 1
    ]

    if not evaluated:
        return None, Decimal("0"), Decimal("0")

    # Sort: priority ASC → discount_amt DESC → eff_pct DESC → name ASC
    evaluated.sort(key=lambda x: (x[0].priority, -x[1], -x[2], x[0].name))
    winning_rule, best_discount, best_pct = evaluated[0]
    return winning_rule, best_discount, best_pct


def pick_stackable_rules(
    applicable_rules: List[DiscountRule],
    item: LineItem
) -> Tuple[List[DiscountRule], Decimal, Decimal]:
    """
    P4: Stackable rule engine.

    Logic:
      - Separate rules into: stackable group vs best-wins group
      - Best-wins group: pick single winner (highest discount, standard logic)
      - Stackable group: apply sequentially on descending net price
      - Compare: stackable total vs best-wins single. Return whichever benefits customer more.
      - IMPORTANT: stackable rules apply on the net-after-previous-discount price,
        so the effective % compounds correctly.

    Returns: (winning_rules_list, total_discount_amount, effective_total_pct)
    """
    if not applicable_rules:
        return [], Decimal("0"), Decimal("0")

    stackable_rules = [r for r in applicable_rules if r.stackable and r.priority != 1]
    normal_rules    = [r for r in applicable_rules if not r.stackable]

    # Special price always wins regardless
    specials = [r for r in applicable_rules if r.priority == 1]
    if specials:
        rule = specials[0]
        disc, pct = compute_discount(rule, item)
        return [rule], disc, pct

    # Best-wins path (normal rules)
    best_single_rule = None
    best_single_disc = Decimal("0")
    best_single_pct  = Decimal("0")
    if normal_rules:
        best_single_rule, best_single_disc, best_single_pct = pick_best_rule(normal_rules, item)

    # Stackable path: apply rules sorted by priority then discount desc
    stacked_disc = Decimal("0")
    current_net  = item.gross
    fired_rules  = []
    if stackable_rules:
        sorted_stackable = sorted(
            stackable_rules,
            key=lambda r: (r.priority, -compute_discount(r, item)[0], r.name)
        )
        for rule in sorted_stackable:
            # Compute discount on CURRENT net (compounding)
            if rule.value_type == ValueType.PERCENT and rule.value is not None:
                disc = (current_net * rule.value / 100).quantize(TWO_PLACES)
                pct_on_current = rule.value
            elif rule.value_type == ValueType.FIXED and rule.value is not None:
                disc = min(rule.value, current_net).quantize(TWO_PLACES)
                pct_on_current = (disc / current_net * 100).quantize(TWO_PLACES) if current_net else Decimal("0")
            else:
                disc, _ = compute_discount(rule, item)
                pct_on_current = Decimal("0")

            if disc > Decimal("0"):
                stacked_disc  += disc
                current_net   -= disc
                fired_rules.append(rule)

    stacked_eff_pct = (stacked_disc / item.gross * 100).quantize(TWO_PLACES) if item.gross else Decimal("0")

    # Pick whichever gives more benefit to customer
    if not fired_rules:
        if best_single_rule:
            return [best_single_rule], best_single_disc, best_single_pct
        return [], Decimal("0"), Decimal("0")

    if stacked_disc >= best_single_disc:
        return fired_rules, stacked_disc, stacked_eff_pct
    else:
        return [best_single_rule] if best_single_rule else [], best_single_disc, best_single_pct


# ────────────────────────────────────────────────────────────────────────────
# STEP 4 — GST
# ────────────────────────────────────────────────────────────────────────────

def apply_gst(net_amount: Decimal, gst_rate: Decimal) -> Decimal:
    """GST always on post-discount net amount."""
    return (net_amount * gst_rate / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ────────────────────────────────────────────────────────────────────────────
# STEP 5 — MARGIN  (P3: tiered)
# ────────────────────────────────────────────────────────────────────────────

def compute_margin(
    net_amount:          Decimal,
    cost_gross:          Optional[Decimal],
    soft_warn_pct:       Decimal = DEFAULT_MARGIN_SOFT_WARN_PCT,
    hard_stop_pct:       Decimal = DEFAULT_MARGIN_HARD_STOP_PCT,
) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    """
    P3: Tiered margin computation.

    Returns: (margin_amount, margin_pct, margin_status)
    margin_status is one of: "ok" | "soft_warning" | "hard_stop"

    The caller (DiscountEngine.calculate) decides what to do with hard_stop:
    - raise MarginHardStopError (strict mode)
    - just surface the status (permissive mode)
    """
    if cost_gross is None or net_amount <= Decimal("0"):
        return None, None, MARGIN_OK

    margin_amount = (net_amount - cost_gross).quantize(TWO_PLACES)
    margin_pct    = (margin_amount / net_amount * 100).quantize(TWO_PLACES) if net_amount else Decimal("0")

    if margin_pct < hard_stop_pct:
        status = MARGIN_HARD_STOP
    elif margin_pct < soft_warn_pct:
        status = MARGIN_SOFT_WARNING
    else:
        status = MARGIN_OK

    return margin_amount, margin_pct, status


# ────────────────────────────────────────────────────────────────────────────
# P5: RULE CACHE  — compiled at init, O(1) channel+type lookup
# ────────────────────────────────────────────────────────────────────────────

def _build_rule_cache(rules: List[DiscountRule]) -> Dict[str, List[DiscountRule]]:
    """
    P5: Build a lightweight index keyed by SalesChannel value.
    Rules with channel=ALL appear in every bucket.

    Cache structure: {"wholesale": [...], "retail": [...], "online": [...], "all": [...]}
    Reduces the filter_rules scan from O(all_rules) to O(channel_rules).
    """
    cache: Dict[str, List[DiscountRule]] = {
        "wholesale": [],
        "retail":    [],
        "online":    [],
        "all":       [],
    }
    for rule in rules:
        if not rule.active:
            continue
        ch = rule.conditions.channel.value
        if ch == "all":
            # ALL channel rules go into every bucket
            for bucket in cache.values():
                bucket.append(rule)
        elif ch in cache:
            cache[ch].append(rule)
            cache["all"].append(rule)   # "all" bucket = union of everything
    return cache


# ────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ────────────────────────────────────────────────────────────────────────────

class DiscountEngine:
    """
    Optical Discount Engine v2.1 — polished.

    Args:
        rules              — list of DiscountRule (active ones are filtered at init)
        soft_warn_pct      — margin % below which a soft warning is raised (default 15)
        hard_stop_pct      — margin % below which hard stop triggers (default 5)
        raise_on_hard_stop — if True, calculate() raises MarginHardStopError on hard stop
                             if False, it surfaces margin_status="hard_stop" in result
                             Default: False (permissive — let billing system decide)

    Usage:
        engine = DiscountEngine(rules)
        result = engine.calculate(item)
        logger.debug(result.pretty())
        logger.debug(result.margin_status)   # "ok" | "soft_warning" | "hard_stop"
    """

    def __init__(
        self,
        rules:              List[DiscountRule],
        soft_warn_pct:      Decimal = DEFAULT_MARGIN_SOFT_WARN_PCT,
        hard_stop_pct:      Decimal = DEFAULT_MARGIN_HARD_STOP_PCT,
        raise_on_hard_stop: bool    = False,
    ):
        self.rules              = [r for r in rules if r.active]
        self.soft_warn_pct      = soft_warn_pct
        self.hard_stop_pct      = hard_stop_pct
        self.raise_on_hard_stop = raise_on_hard_stop
        # P5: build cache at init
        self._cache             = _build_rule_cache(self.rules)

    def _get_rules_for_channel(self, channel: SalesChannel) -> List[DiscountRule]:
        """P5: O(1) channel lookup via pre-built cache."""
        return self._cache.get(channel.value, self._cache["all"])

    def calculate(self, item: LineItem) -> DiscountResult:
        """
        Full pipeline: Filter → BestWins/Stackable → Discount → GST → Margin → Result

        Raises MarginHardStopError if raise_on_hard_stop=True and margin < hard_stop_pct.
        """
        gross = item.gross

        # P5: use cache for fast channel-scoped rule lookup
        channel_rules = self._get_rules_for_channel(item.channel)
        applicable    = filter_rules(channel_rules, item)

        # P4: route to stackable or best-wins engine
        has_stackable = any(r.stackable for r in applicable)
        if has_stackable:
            winning_rules, discount_amt, discount_pct = pick_stackable_rules(applicable, item)
            winning_rule = winning_rules[0] if winning_rules else None
            rule_name    = (
                " + ".join(r.name for r in winning_rules)
                if len(winning_rules) > 1
                else (winning_rule.name if winning_rule else "No Discount")
            )
        else:
            winning_rule, discount_amt, discount_pct = pick_best_rule(applicable, item)
            winning_rules = [winning_rule] if winning_rule else []
            rule_name     = winning_rule.name if winning_rule else "No Discount"

        net      = (gross - discount_amt).quantize(TWO_PLACES)
        gst_rate = item.gst_rate if item.gst_rate is not None else (
            winning_rule.gst_rate if winning_rule else Decimal("12")
        )
        gst_amt  = apply_gst(net, gst_rate)
        final    = (net + gst_amt).quantize(TWO_PLACES)

        # P3: tiered margin
        margin_amt, margin_pct, margin_status = compute_margin(
            net, item.cost_gross, self.soft_warn_pct, self.hard_stop_pct
        )

        # P3: optionally raise hard stop
        if self.raise_on_hard_stop and margin_status == MARGIN_HARD_STOP and winning_rule:
            raise MarginHardStopError(rule_name, margin_pct or Decimal("0"), self.hard_stop_pct)

        return DiscountResult(
            base_price      = item.base_price,
            quantity        = item.quantity,
            gross_amount    = gross,
            rule_applied    = winning_rule,
            rule_name       = rule_name,
            discount_pct    = discount_pct,
            discount_amount = discount_amt,
            net_amount      = net,
            gst_rate        = gst_rate,
            gst_amount      = gst_amt,
            final_amount    = final,
            evaluated_rules = applicable,
            cost_gross      = item.cost_gross,
            margin_amount   = margin_amt,
            margin_pct      = margin_pct,
            margin_status   = margin_status,
        )

    def simulate(self, item: LineItem) -> dict:
        """
        Full simulation — all evaluated rules, winner, and margin data.
        Use for preview panel / sales training mode.
        """
        result        = self.calculate(item)
        channel_rules = self._get_rules_for_channel(item.channel)
        applicable    = filter_rules(channel_rules, item)
        all_evaluated = []

        winner_ids = {r.id for r in (result.evaluated_rules if result.rule_applied else [])}

        for rule in applicable:
            disc_amt, eff_pct = compute_discount(rule, item)
            net  = item.gross - disc_amt
            gst  = apply_gst(net, rule.gst_rate)
            m_amt, m_pct, m_status = compute_margin(
                net, item.cost_gross, self.soft_warn_pct, self.hard_stop_pct
            )
            all_evaluated.append({
                "rule_id":        rule.id,
                "rule_name":      rule.name,
                "rule_type":      rule.type.value,
                "priority":       rule.priority,
                "stackable":      rule.stackable,
                "discount_pct":   float(eff_pct),
                "discount_amt":   float(disc_amt),
                "net_amount":     float(net),
                "gst_amount":     float(gst),
                "final_amount":   float(net + gst),
                "margin_pct":     float(m_pct) if m_pct is not None else None,
                "margin_status":  m_status,
                "is_winner":      rule.id == (result.rule_applied.id if result.rule_applied else None),
            })

        return {
            "input": {
                "base_price":   float(item.base_price),
                "quantity":     item.quantity,
                "product_cat":  item.product_cat,
                "brand_group":  item.brand_group,
                "party_tags":   item.party_tags,
                "channel":      item.channel.value,
                "promo_code":   _normalize_promo(item.promo_code),
                "gross":        float(item.gross),
            },
            "winner": result.to_dict(),
            "all_evaluated": sorted(all_evaluated, key=lambda x: (-x["is_winner"], x["priority"])),
            "pipeline": "INPUT → Cache Lookup → Filter → Best-Wins/Stackable → GST → Margin[Tiered] → Invoice",
        }

    def calculate_invoice(self, items: List[LineItem]) -> dict:
        """Calculate discounts for all lines in an invoice."""
        lines          = []
        total_gross    = Decimal("0")
        total_discount = Decimal("0")
        total_gst      = Decimal("0")
        total_final    = Decimal("0")
        total_margin   = Decimal("0")
        has_margin     = False
        worst_status   = MARGIN_OK

        STATUS_RANK = {MARGIN_OK: 0, MARGIN_SOFT_WARNING: 1, MARGIN_HARD_STOP: 2}

        for item in items:
            result = self.calculate(item)
            lines.append(result.to_dict())
            total_gross    += result.gross_amount
            total_discount += result.discount_amount
            total_gst      += result.gst_amount
            total_final    += result.final_amount
            if result.margin_amount is not None:
                total_margin += result.margin_amount
                has_margin    = True
            if STATUS_RANK.get(result.margin_status, 0) > STATUS_RANK.get(worst_status, 0):
                worst_status = result.margin_status

        totals: dict = {
            "gross":          float(total_gross),
            "total_discount": float(total_discount),
            "total_gst":      float(total_gst),
            "payable":        float(total_final),
        }
        if has_margin:
            totals["total_margin"]  = float(total_margin)
            totals["margin_status"] = worst_status

        return {"lines": lines, "totals": totals}

    def list_available_offers(self, item: LineItem) -> List[dict]:
        """
        Return all offers visible to this customer for the given item.
        Used for collapsible "Available Offers" panel (retail / online).

        Does NOT require promo_code match — returns all applicable offers
        including those that need a code, so the customer can see what codes exist.
        """
        # Build promo-stripped item for offer discovery
        item_no_promo = LineItem(
            base_price  = item.base_price,
            quantity    = item.quantity,
            product_id  = item.product_id,
            product_cat = item.product_cat,
            party_id    = item.party_id,
            party_tags  = item.party_tags,
            brand_group = item.brand_group,
            channel     = item.channel,
            promo_code  = None,
            cost_price  = item.cost_price,
        )

        offers = []
        for rule in self.rules:
            if not rule.show_in_offers or not rule.active:
                continue

            # Check all conditions except promo_code
            from dataclasses import replace
            cond_no_promo = RuleConditions(**{**rule.conditions.__dict__, "promo_code": None})
            rule_no_promo = DiscountRule(**{**rule.__dict__, "conditions": cond_no_promo})
            if not filter_rules([rule_no_promo], item_no_promo):
                continue

            disc_amt, eff_pct = compute_discount(rule, item)
            offer: dict = {
                "rule_id":       rule.id,
                "name":          rule.name,
                "description":   rule.description,
                "display_label": rule.display_label or rule.name,
                "icon":          rule.icon_emoji,
                "type":          rule.type.value,
                "channel":       rule.conditions.channel.value,
                "discount_pct":  float(eff_pct),
                "requires_code": bool(rule.conditions.promo_code),
                "promo_code":    _normalize_promo(rule.conditions.promo_code),
                "valid_to":      rule.conditions.valid_to.isoformat() if rule.conditions.valid_to else None,
                "stackable":     rule.stackable,
            }
            if rule.type == RuleType.OFFER_BOGO and rule.bogo_buy:
                offer["bogo_label"] = f"Buy {rule.bogo_buy} Get {rule.bogo_get} Free"
            offers.append(offer)

        return sorted(offers, key=lambda x: (-x["discount_pct"], x["name"]))
