"""
modules/pricing — Optical Discount & Pricing Engine
=====================================================

PUBLIC API — import from here:

    from modules.pricing import DiscountEngine, LineItem, DiscountRule
    from modules.pricing import compute_line_totals, apply_taxes
    from modules.pricing import WHOLESALE_POLICY, RETAIL_POLICY
    from modules.pricing import DSL, DecisionLogger

FILES & WHAT THEY DO
--------------------
engine.py
    DiscountEngine        — main class: filter→pick→GST→margin pipeline
    filter_rules()        — step 1: which rules apply to this line item
    compute_discount()    — step 2: calculate discount amount for a rule
    pick_best_rule()      — step 3: best-wins selection (priority ladder)
    pick_stackable_rules()— step 4: sequential stacking for stackable rules
    apply_gst()           — step 5: GST on post-discount net
    compute_margin()      — step 6: tiered margin check (ok/soft_warning/hard_stop)

discount_rule.py
    DiscountRule          — one discount rule (dataclass with from_dict/to_dict)
    LineItem              — input to engine for one billing line
    DiscountResult        — output from engine.calculate() per line
    RuleType              — party/product/brand_group/special/offer_bogo/offer_slab/coating/promo_code
    ValueType             — percent/fixed/special_price/bogo
    SalesChannel          — wholesale/retail/online/all
    ConflictStrategy      — best_price/highest_priority/stack/margin_safe
    RuleConditions        — all filter conditions (party, product, brand, date, qty, channel, promo)
    SlabTier              — one quantity tier in a slab rule

discount_adapter.py
    DiscountAdapter       — PostgreSQL CRUD for discount_rules table
      .get_active_rules() — fetch all active rules (optionally by type)
      .get_rule_by_id()   — single rule
      .list_all_rules()   — raw dicts for admin listing
      .create_rule()      — insert new rule (returns UUID)
      .update_rule()      — update fields (partial update)
      .deactivate_rule()  — soft delete (active=FALSE)
      .log_application()  — audit trail per invoice line
      .get_discount_history() — all decisions for an invoice

pricing_policy.py
    PricingPolicy         — named policy (namespace + channel + margin thresholds)
      .build_engine()     — scopes rules to this policy, returns DiscountEngine
    WHOLESALE_POLICY      — B2B: warn<12%, stop<4%
    RETAIL_POLICY         — OTC: warn<20%, stop<8%
    ONLINE_POLICY         — App: warn<18%, stop<6%
    FRANCHISE_POLICY      — Franchise: warn<25%, stop<10%, raises on hard stop
    get_policy(channel)   — returns the right policy for a channel

billing_engine.py
    compute_line_totals() — per-line: picks price by order_type, normalises
                            BOX→PCS, applies GST (inclusive for RETAIL)

tax_engine.py
    apply_taxes()         — stamps gst_amount on every line, returns order
    resolve_gst_percent() — priority: line.gst_percent → history table → 0

price_resolver.py
    resolve_price_for_order_type() — picks correct price column by order type
      WHOLESALE → selling_price; RETAIL → mrp; PURCHASE → purchase_rate
    get_price_field_name()  — returns primary column name for logging

decision_logger.py
    DecisionLogger        — logs every discount decision to discount_decisions
      .log()              — one line decision
      .log_invoice()      — all lines in one call
      .get_invoice_decisions() — retrieve for audit display
      .get_rule_stats()   — analytics: fire count, avg margin, hard_stop count

condition_dsl.py
    DSL                   — fluent builder for condition DSL dicts
      DSL.all([...])      — AND block
      DSL.any([...])      — OR block
      DSL.brand_in([])    — brand_group condition
      DSL.party_tags_any([]) — party tag condition
      DSL.min_qty(n)      — minimum quantity
      DSL.channel_is(c)   — channel condition
      DSL.promo_code(c)   — promo code match
      DSL.min_margin(pct) — only fire if margin >= pct%
    matches_dsl(dsl, item)— evaluate DSL against a LineItem

live_adapter.py
    build_live_engine()   — load active DB policy → build DiscountEngine
    load_active_policy()  — fetch pricing policy from DB
    load_rules_for_policy()— fetch rules linked to a policy

shadow_mode.py
    run_shadow()          — run new engine alongside legacy (no billing impact)
    shadow_enabled()      — checks PRICING_SHADOW_MODE env var

pricing_pipeline.py
    PricingPipeline       — policy → engine → logger → deterministic hash
      .price(PricingInput)→ PricingOutput

pricing_engine.py (legacy)
    money()               — Decimal-safe 2dp rounding
    compute_weighted_price() — weighted avg price across batches
    apply_pricing_line()  — stamp unit_price + total_price from batches
    validate_pricing_line()  — validate unit/total consistency
    validate_batch_pricing_data() — validate batch data completeness

HOW TO USE
----------

# Simple discount calculation:
from modules.pricing import DiscountEngine, LineItem, DiscountRule, RuleType, ValueType, RuleConditions, SalesChannel
from decimal import Decimal

rule = DiscountRule(
    id="r1", name="Wholesale 12%",
    type=RuleType.PARTY, value_type=ValueType.PERCENT,
    value=Decimal("12"), gst_rate=Decimal("12"),
    conditions=RuleConditions(party_tags=["wholesale"]),
)
engine = DiscountEngine([rule])
item   = LineItem(base_price=Decimal("1000"), quantity=10,
                  party_tags=["wholesale"], channel=SalesChannel.WHOLESALE)
result = engine.calculate(item)
logger.debug(result.pretty())

# Using a policy:
from modules.pricing import WHOLESALE_POLICY
engine = WHOLESALE_POLICY.build_engine(all_rules)
result = engine.calculate(item)

# Simulate all rules (for UI preview):
sim = engine.simulate(item)
# sim["all_evaluated"] → list of all rules with winner flagged

# Full invoice:
results_dict = engine.calculate_invoice([item1, item2, item3])
# results_dict["totals"]["payable"]

# Billing line totals (box-aware):
from modules.pricing import compute_line_totals
t = compute_line_totals(
    quantity=6, box_size=6,
    selling_price=600, mrp=720, purchase_rate=480,
    order_type="RETAIL", gst_percent=12
)
# t = {"unit_price": 120, "base": 720, "tax": 77.14, "total": 720}
"""

from .engine          import (DiscountEngine, MarginHardStopError,
                               filter_rules, compute_discount, pick_best_rule,
                               pick_stackable_rules, apply_gst, compute_margin)
from .discount_rule   import (DiscountRule, LineItem, DiscountResult,
                               RuleType, ValueType, SalesChannel, ConflictStrategy,
                               RuleConditions, SlabTier, RULE_PRIORITY_MAP)
from .pricing_policy  import (PricingPolicy, get_policy,
                               WHOLESALE_POLICY, RETAIL_POLICY,
                               ONLINE_POLICY, FRANCHISE_POLICY, CHANNEL_POLICY_MAP)
from .billing_engine  import compute_line_totals
from .tax_engine      import apply_taxes, resolve_gst_percent
from .price_resolver  import resolve_price_for_order_type, get_price_field_name
from .decision_logger import DecisionLogger, DecisionRecord
from .condition_dsl   import DSL, matches_dsl, eval_dsl
from .pricing_engine  import (money, compute_weighted_price, apply_pricing_line,
                               validate_pricing_line, validate_batch_pricing_data)
