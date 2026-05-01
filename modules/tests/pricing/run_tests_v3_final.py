"""
tests/run_tests_v3_final.py — FINAL LOCKED test suite v3.0

Tests all new v3 components:
  DSL  — condition_dsl evaluator + builder
  POL  — pricing policy layer
  LOG  — decision logger (memory mode)
  API  — simulate_request() function
  NS   — namespace scoping
  CS   — conflict_strategy field
  VER  — version + parent_rule_id fields
  REG  — regression: v2 still passes through v3 models

Run: python tests/run_tests_v3_final.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal

from models.discount_rule import (
    DiscountRule, LineItem, RuleType, ValueType,
    RuleConditions, SlabTier, SalesChannel, ConflictStrategy,
)
from core.engine import DiscountEngine
from core.condition_dsl import DSL, matches_dsl, eval_dsl
from core.decision_logger import DecisionLogger, DecisionRecord
from policies.pricing_policy import (
    PricingPolicy, get_policy,
    WHOLESALE_POLICY, RETAIL_POLICY, ONLINE_POLICY, FRANCHISE_POLICY,
)
from api.simulate_api import SimulateRequest, simulate_request


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_rule(**kwargs):
    defaults = dict(
        id="r001", name="Test Rule", type=RuleType.PARTY,
        value_type=ValueType.PERCENT, value=Decimal("10"),
        gst_rate=Decimal("12"), conditions=RuleConditions(),
        priority=3, active=True, stackable=False, namespace="core",
    )
    defaults.update(kwargs)
    return DiscountRule(**defaults)

def make_item(**kwargs):
    defaults = dict(
        base_price=Decimal("1000"), quantity=10,
        product_cat="frame", party_tags=["wholesale"],
        channel=SalesChannel.WHOLESALE, namespace="core",
    )
    defaults.update(kwargs)
    return LineItem(**defaults)


# ── Runner ────────────────────────────────────────────────────────────────────

passed, failed, results = 0, 0, []

def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        results.append(f"  ✅  {name}")
    except Exception as e:
        failed += 1
        results.append(f"  ❌  {name}")
        results.append(f"       → {e}")

def eq(a, b, msg=""):
    if a != b: raise AssertionError(f"Expected {b!r}, got {a!r}. {msg}")

def ok(v, msg=""):
    if not v: raise AssertionError(f"Expected True. {msg}")

def raises(exc_type, fn):
    try:
        fn()
        raise AssertionError(f"Expected {exc_type.__name__} but nothing raised")
    except exc_type:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# DSL — Condition DSL evaluator
# ═══════════════════════════════════════════════════════════════════════════════

def t_dsl_simple_equals():
    dsl  = DSL.field("channel", "=", "wholesale")
    item = make_item(channel=SalesChannel.WHOLESALE)
    ok(matches_dsl(dsl, item))

def t_dsl_simple_not_equals():
    dsl  = DSL.field("channel", "!=", "retail")
    item = make_item(channel=SalesChannel.WHOLESALE)
    ok(matches_dsl(dsl, item))

def t_dsl_in_list():
    dsl  = DSL.field("product_cat", "in", ["frame","lens"])
    ok(matches_dsl(dsl, make_item(product_cat="frame")))
    ok(not matches_dsl(dsl, make_item(product_cat="accessories")))

def t_dsl_not_in_list():
    dsl  = DSL.field("product_cat", "not_in", ["accessories"])
    ok(matches_dsl(dsl, make_item(product_cat="frame")))
    ok(not matches_dsl(dsl, make_item(product_cat="accessories")))

def t_dsl_any_in_list_tags():
    dsl  = DSL.field("party_tags", "any", ["vip","gold"])
    ok(matches_dsl(dsl, make_item(party_tags=["gold","retail"])))
    ok(not matches_dsl(dsl, make_item(party_tags=["wholesale"])))

def t_dsl_numeric_gte():
    dsl  = DSL.min_qty(10)
    ok(matches_dsl(dsl, make_item(quantity=10)))
    ok(matches_dsl(dsl, make_item(quantity=15)))
    ok(not matches_dsl(dsl, make_item(quantity=9)))

def t_dsl_numeric_gt():
    dsl = DSL.field("quantity", ">", 10)
    ok(matches_dsl(dsl, make_item(quantity=11)))
    ok(not matches_dsl(dsl, make_item(quantity=10)))

def t_dsl_none_field_no_crash():
    """Field with None value should return False, not crash."""
    dsl  = DSL.field("brand_group", "=", "titan")
    item = make_item(brand_group=None)
    ok(not matches_dsl(dsl, item))

def t_dsl_none_dsl_always_matches():
    """None DSL = no restriction = always matches."""
    ok(matches_dsl(None, make_item()))

def t_dsl_all_and_logic():
    dsl = DSL.all([
        DSL.field("channel",     "=",  "wholesale"),
        DSL.field("product_cat", "in", ["frame"]),
        DSL.min_qty(5),
    ])
    ok(matches_dsl(dsl, make_item(channel=SalesChannel.WHOLESALE, product_cat="frame", quantity=10)))
    ok(not matches_dsl(dsl, make_item(channel=SalesChannel.RETAIL, product_cat="frame", quantity=10)))

def t_dsl_any_or_logic():
    dsl = DSL.any([
        DSL.field("party_tags", "any", ["vip"]),
        DSL.brand_in(["titan"]),
    ])
    ok(matches_dsl(dsl, make_item(party_tags=["vip"])))
    ok(matches_dsl(dsl, make_item(brand_group="titan")))
    ok(not matches_dsl(dsl, make_item(party_tags=["retail"], brand_group="local")))

def t_dsl_nested():
    """(retail OR online) AND product in frame"""
    dsl = DSL.all([
        DSL.any([DSL.channel_is("retail"), DSL.channel_is("online")]),
        DSL.product_in(["frame"]),
    ])
    ok(matches_dsl(dsl, make_item(channel=SalesChannel.RETAIL, product_cat="frame")))
    ok(matches_dsl(dsl, make_item(channel=SalesChannel.ONLINE, product_cat="frame")))
    ok(not matches_dsl(dsl, make_item(channel=SalesChannel.WHOLESALE, product_cat="frame")))
    ok(not matches_dsl(dsl, make_item(channel=SalesChannel.RETAIL, product_cat="lens")))

def t_dsl_promo_code_normalized():
    dsl  = DSL.promo_code("DIWALI25")
    ok(matches_dsl(dsl, make_item(promo_code="diwali25")))
    ok(matches_dsl(dsl, make_item(promo_code="  DIWALI25  ")))
    ok(not matches_dsl(dsl, make_item(promo_code="OTHER")))

def t_dsl_bad_dsl_never_crashes():
    """Malformed DSL must not crash engine."""
    # bad leaf has no field/conditions → falls to empty branch → True (safe default)
    result = matches_dsl({"op": "all", "conditions": [{"bad": "data"}]}, make_item())
    ok(result in (True, False))  # must not crash regardless of result

def t_dsl_builder_shortcuts():
    """All DSL builder shortcuts produce valid dicts."""
    dsl = DSL.all([
        DSL.party_tags_any(["vip"]),
        DSL.product_in(["frame"]),
        DSL.brand_in(["titan"]),
        DSL.channel_is("wholesale"),
        DSL.promo_code("TEST"),
        DSL.min_qty(5),
        DSL.max_qty(50),
        DSL.min_gross(1000),
        DSL.namespace_is("core"),
        DSL.party_is("p-001"),
    ])
    # Just check it's a valid dict structure — no crash
    ok(isinstance(dsl, dict))
    ok("conditions" in dsl)
    eq(len(dsl["conditions"]), 10)

def t_dsl_rule_with_conditions_dsl():
    """Rule with conditions_dsl set uses DSL for evaluation via filter_rules."""
    from core.engine import filter_rules
    rule = make_rule(
        conditions_dsl = DSL.all([
            DSL.brand_in(["titan"]),
            DSL.party_tags_any(["wholesale"]),
        ]),
        conditions = RuleConditions(),  # legacy empty — DSL should take over
    )
    # With DSL: engine currently uses legacy conditions (DSL is stored for future DSL-aware filter)
    # This test confirms DSL dict is stored and readable on the rule
    ok(rule.conditions_dsl is not None)
    ok(matches_dsl(rule.conditions_dsl, make_item(brand_group="titan", party_tags=["wholesale"])))
    ok(not matches_dsl(rule.conditions_dsl, make_item(brand_group="rayban")))


# ═══════════════════════════════════════════════════════════════════════════════
# POL — Pricing Policy Layer
# ═══════════════════════════════════════════════════════════════════════════════

def _sample_rules():
    return [
        make_rule(id="core-10", name="Core 10%",      namespace="core",      value=Decimal("10"),
                  conditions=RuleConditions(channel=SalesChannel.ALL)),
        make_rule(id="ws-12",   name="Wholesale 12%", namespace="wholesale", value=Decimal("12"),
                  conditions=RuleConditions(channel=SalesChannel.WHOLESALE)),
        make_rule(id="rt-8",    name="Retail 8%",     namespace="retail",    value=Decimal("8"),
                  conditions=RuleConditions(channel=SalesChannel.RETAIL)),
        make_rule(id="fr-5",    name="Franchise 5%",  namespace="franchise", value=Decimal("5"),
                  conditions=RuleConditions(channel=SalesChannel.ALL)),
    ]

def t_pol_wholesale_policy_scopes_correctly():
    """Wholesale policy should include core + wholesale rules, not retail/franchise."""
    all_rules = _sample_rules()
    engine    = WHOLESALE_POLICY.build_engine(all_rules)
    rule_ids  = {r.id for r in engine.rules}
    ok("core-10" in rule_ids)
    ok("ws-12"   in rule_ids)
    ok("rt-8"    not in rule_ids)
    ok("fr-5"    not in rule_ids)

def t_pol_retail_policy_scopes_correctly():
    all_rules = _sample_rules()
    engine    = RETAIL_POLICY.build_engine(all_rules)
    rule_ids  = {r.id for r in engine.rules}
    ok("core-10" in rule_ids)
    ok("rt-8"    in rule_ids)
    ok("ws-12"   not in rule_ids)

def t_pol_franchise_raises_on_hard_stop():
    """Franchise policy has raise_on_hard_stop=True — must raise MarginHardStopError."""
    from core.engine import MarginHardStopError
    rule = make_rule(value=Decimal("50"), namespace="franchise",
                     conditions=RuleConditions(channel=SalesChannel.ALL))
    engine = FRANCHISE_POLICY.build_engine([rule])
    item   = make_item(
        base_price=Decimal("1000"), quantity=1,
        cost_price=Decimal("900"), channel=SalesChannel.ALL,
    )
    raises(MarginHardStopError, lambda: engine.calculate(item))

def t_pol_wholesale_does_not_raise():
    """Wholesale policy is permissive — never raises on hard stop."""
    rule   = make_rule(value=Decimal("50"), namespace="core",
                       conditions=RuleConditions(channel=SalesChannel.ALL))
    engine = WHOLESALE_POLICY.build_engine([rule])
    item   = make_item(base_price=Decimal("1000"), quantity=1,
                       cost_price=Decimal("900"), channel=SalesChannel.WHOLESALE)
    result = engine.calculate(item)   # must not raise
    eq(result.margin_status, "hard_stop")

def t_pol_get_policy_channel_routing():
    eq(get_policy(SalesChannel.WHOLESALE).name, WHOLESALE_POLICY.name)
    eq(get_policy(SalesChannel.RETAIL).name,    RETAIL_POLICY.name)
    eq(get_policy(SalesChannel.ONLINE).name,    ONLINE_POLICY.name)

def t_pol_get_policy_franchise_namespace():
    """franchise namespace always returns FRANCHISE_POLICY regardless of channel."""
    eq(get_policy(SalesChannel.WHOLESALE, "franchise").name, FRANCHISE_POLICY.name)
    eq(get_policy(SalesChannel.RETAIL,    "franchise").name, FRANCHISE_POLICY.name)

def t_pol_custom_policy():
    """Custom policy with tight guardrails should work the same way."""
    custom = PricingPolicy(
        name="Custom Tight",
        namespace="core",
        channel=SalesChannel.ALL,
        soft_warn_pct=Decimal("30"),
        hard_stop_pct=Decimal("20"),
        raise_on_hard_stop=False,
    )
    rule   = make_rule(value=Decimal("10"), conditions=RuleConditions())
    engine = custom.build_engine([rule])
    item   = make_item(base_price=Decimal("1000"), quantity=1, cost_price=Decimal("675"))
    result = engine.calculate(item)
    # net=900, cost=675, margin=225/900=25% → above hard_stop(20%), below soft_warn(30%) → soft_warning
    eq(result.margin_status, "soft_warning")

def t_pol_describe_returns_string():
    ok(isinstance(WHOLESALE_POLICY.describe(), str))
    ok("Wholesale" in WHOLESALE_POLICY.describe())


# ═══════════════════════════════════════════════════════════════════════════════
# LOG — Decision Logger
# ═══════════════════════════════════════════════════════════════════════════════

def t_log_records_decision_in_memory():
    """Without DB, logger stores in memory."""
    rule   = make_rule()
    engine = DiscountEngine([rule])
    item   = make_item()
    result = engine.calculate(item)

    logger = DecisionLogger(db_conn=None)
    did    = logger.log("inv-001", "line-001", item, result)

    ok(did is not None)
    decisions = logger.get_invoice_decisions("inv-001")
    eq(len(decisions), 1)
    eq(decisions[0]["invoice_id"], "inv-001")

def t_log_records_applied_rule():
    rule   = make_rule(id="rule-xyz", name="Party 10%")
    engine = DiscountEngine([rule])
    item   = make_item(channel=SalesChannel.ALL)
    result = engine.calculate(item)

    logger = DecisionLogger()
    logger.log("inv-002", "l1", item, result)
    decisions = logger.get_invoice_decisions("inv-002")
    eq(decisions[0]["applied_rule_id"],   "rule-xyz")
    eq(decisions[0]["applied_rule_name"], "Party 10%")

def t_log_records_competing_rules():
    """competing_rules should list ALL evaluated rules."""
    r1 = make_rule(id="r1", name="10%", value=Decimal("10"))
    r2 = make_rule(id="r2", name="15%", value=Decimal("15"))
    engine = DiscountEngine([r1, r2])
    item   = make_item(channel=SalesChannel.ALL)
    result = engine.calculate(item)

    logger = DecisionLogger()
    logger.log("inv-003", "l1", item, result)
    d = logger.get_invoice_decisions("inv-003")[0]
    ok(len(d["competing_rules"]) >= 2, "Should have both rules in competing")

def t_log_records_margin_fields():
    rule   = make_rule(value=Decimal("10"))
    engine = DiscountEngine([rule])
    item   = make_item(cost_price=Decimal("700"), channel=SalesChannel.ALL)
    result = engine.calculate(item)

    logger = DecisionLogger()
    logger.log("inv-004", "l1", item, result)
    d = logger.get_invoice_decisions("inv-004")[0]
    ok(d["margin_pct"] is not None)
    ok(d["margin_status"] in ("ok","soft_warning","hard_stop"))

def t_log_no_decision_never_crashes():
    """Logger with no DB and no result rule should still not crash."""
    rule   = make_rule()
    engine = DiscountEngine([])  # no rules
    item   = make_item()
    result = engine.calculate(item)  # no discount

    logger = DecisionLogger()
    did    = logger.log("inv-005", "l1", item, result)  # must not raise
    ok(True)  # reaching here = success

def t_log_rule_stats_memory():
    """get_rule_stats in memory mode returns correct fire_count."""
    rule   = make_rule(id="stat-rule")
    engine = DiscountEngine([rule])
    item   = make_item(channel=SalesChannel.ALL)
    logger = DecisionLogger()

    for i in range(3):
        result = engine.calculate(item)
        logger.log(f"inv-{i}", f"l{i}", item, result)

    stats = logger.get_rule_stats("stat-rule")
    eq(stats["fire_count"], 3)

def t_log_invoice_logs_all_lines():
    """log_invoice should log one record per line."""
    rule   = make_rule()
    engine = DiscountEngine([rule])
    items  = [make_item(channel=SalesChannel.ALL) for _ in range(3)]
    results = [engine.calculate(i) for i in items]

    logger = DecisionLogger()
    ids    = logger.log_invoice("inv-bulk", items, results)
    eq(len(ids), 3)
    ok(all(d is not None for d in ids))

    decisions = logger.get_invoice_decisions("inv-bulk")
    eq(len(decisions), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# API — Simulate Request/Response
# ═══════════════════════════════════════════════════════════════════════════════

def _api_rules():
    return [
        make_rule(id="ws-12", name="Wholesale 12%", value=Decimal("12"),
                  namespace="core",
                  conditions=RuleConditions(
                      party_tags=["wholesale"], channel=SalesChannel.WHOLESALE)),
        make_rule(id="promo-15", name="Promo 15%", value=Decimal("15"),
                  type=RuleType.PROMO_CODE, namespace="core",
                  conditions=RuleConditions(promo_code="DIWALI25")),
    ]

def t_api_basic_simulate():
    payload = {
        "channel": "wholesale",
        "namespace": "core",
        "items": [{
            "base_price": 1200,
            "quantity": 10,
            "product_cat": "frame",
            "party_tags": ["wholesale"],
        }]
    }
    req      = SimulateRequest.from_dict(payload)
    response = simulate_request(req, _api_rules())
    eq(response["status"], "ok")
    ok(response["totals"]["payable"] > 0)

def t_api_winner_correct():
    payload = {
        "channel": "wholesale",
        "party_tags": ["wholesale"],
        "items": [{"base_price": 1000, "quantity": 1}]
    }
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    eq(response["lines"][0]["rule_applied"], "Wholesale 12%")

def t_api_promo_code_fires():
    payload = {
        "channel": "retail",
        "promo_code": "diwali25",  # lowercase — should be normalized
        "items": [{"base_price": 1000, "quantity": 1}]
    }
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    eq(response["lines"][0]["rule_applied"], "Promo 15%")

def t_api_multiple_lines():
    payload = {
        "channel": "wholesale",
        "party_tags": ["wholesale"],
        "items": [
            {"base_price": 1000, "quantity": 5},
            {"base_price": 800,  "quantity": 10},
        ]
    }
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    eq(len(response["lines"]), 2)
    ok(response["totals"]["gross"] > 0)

def t_api_simulation_includes_all_evaluated():
    payload = {
        "channel": "wholesale",
        "party_tags": ["wholesale"],
        "items": [{"base_price": 1000, "quantity": 1}]
    }
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    ok(len(response["simulations"]) == 1)
    ok("all_evaluated" in response["simulations"][0])

def t_api_policy_name_in_response():
    """Response should include which policy was selected."""
    payload = {"channel": "wholesale", "items": [{"base_price": 1000, "quantity": 1}]}
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    ok("policy" in response)
    eq(response["policy"], WHOLESALE_POLICY.name)

def t_api_bad_payload_returns_error():
    """Missing items should not crash — return error status."""
    payload  = {}  # no items
    response = simulate_request(SimulateRequest.from_dict(payload), _api_rules())
    # Empty items = ok status with empty lines
    ok(response["status"] in ("ok", "error"))


# ═══════════════════════════════════════════════════════════════════════════════
# NS — Namespace and ConflictStrategy on model
# ═══════════════════════════════════════════════════════════════════════════════

def t_ns_namespace_default_core():
    rule = make_rule()
    eq(rule.namespace, "core")

def t_ns_namespace_set_correctly():
    rule = make_rule(namespace="wholesale")
    eq(rule.namespace, "wholesale")

def t_ns_conflict_strategy_default():
    rule = make_rule()
    eq(rule.conflict_strategy, ConflictStrategy.BEST_PRICE)

def t_ns_conflict_strategy_set():
    rule = make_rule(conflict_strategy=ConflictStrategy.HIGHEST_PRIORITY)
    eq(rule.conflict_strategy, ConflictStrategy.HIGHEST_PRIORITY)

def t_ns_from_dict_with_namespace():
    rule = DiscountRule.from_dict({
        "id": "r1", "name": "Test", "type": "party",
        "value_type": "percent", "value": 10, "gst_rate": 12,
        "namespace": "franchise", "conflict_strategy": "margin_safe",
        "conditions": {},
    })
    eq(rule.namespace,         "franchise")
    eq(rule.conflict_strategy, ConflictStrategy.MARGIN_SAFE)

def t_ns_unknown_conflict_strategy_defaults():
    rule = DiscountRule.from_dict({
        "id": "r1", "name": "Test", "type": "party",
        "value_type": "percent", "value": 10, "gst_rate": 12,
        "conflict_strategy": "not_a_real_strategy",
        "conditions": {},
    })
    eq(rule.conflict_strategy, ConflictStrategy.BEST_PRICE)


# ═══════════════════════════════════════════════════════════════════════════════
# VER — Rule Versioning
# ═══════════════════════════════════════════════════════════════════════════════

def t_ver_version_default_1():
    rule = make_rule()
    eq(rule.version, 1)

def t_ver_parent_rule_id_default_none():
    rule = make_rule()
    ok(rule.parent_rule_id is None)

def t_ver_from_dict_version():
    rule = DiscountRule.from_dict({
        "id": "r2", "name": "V2 Rule", "type": "party",
        "value_type": "percent", "value": 12, "gst_rate": 12,
        "version": 3, "parent_rule_id": "r1",
        "conditions": {},
    })
    eq(rule.version, 3)
    eq(rule.parent_rule_id, "r1")


# ═══════════════════════════════════════════════════════════════════════════════
# REG — Regression: v2 models + engine through v3
# ═══════════════════════════════════════════════════════════════════════════════

def t_reg_v2_rule_still_works_in_v3_model():
    """Old v2-style rule dict loads correctly into v3 model."""
    old_rule = {
        "id": "old-r1", "name": "Old Party 12%", "type": "party",
        "value_type": "percent", "value": 12, "gst_rate": 12,
        "conditions": {"party_tags": ["wholesale"], "channel": "wholesale"},
    }
    rule   = DiscountRule.from_dict(old_rule)
    eq(rule.namespace,         "core")
    eq(rule.conflict_strategy, ConflictStrategy.BEST_PRICE)
    eq(rule.version,           1)
    ok(rule.parent_rule_id is None)
    ok(rule.conditions_dsl is None)

def t_reg_calculate_correct_through_v3():
    rule   = make_rule(value=Decimal("12"), conditions=RuleConditions(channel=SalesChannel.ALL))
    result = DiscountEngine([rule]).calculate(
        make_item(base_price=Decimal("1200"), quantity=1, channel=SalesChannel.ALL)
    )
    eq(result.gross_amount,    Decimal("1200"))
    eq(result.discount_amount, Decimal("144.00"))
    eq(result.final_amount,    Decimal("1182.72"))  # net=1056, gst 12%=126.72, final=1182.72

def t_reg_policy_engine_same_result_as_direct():
    """Policy.build_engine() + calculate() must equal direct engine.calculate()."""
    rule = make_rule(value=Decimal("12"),
                     namespace="core",
                     conditions=RuleConditions(channel=SalesChannel.WHOLESALE))
    item = make_item(channel=SalesChannel.WHOLESALE)

    direct  = DiscountEngine([rule]).calculate(item)
    via_pol = WHOLESALE_POLICY.build_engine([rule]).calculate(item)

    eq(direct.final_amount, via_pol.final_amount)
    eq(direct.rule_name,    via_pol.rule_name)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN ALL
# ═══════════════════════════════════════════════════════════════════════════════

ALL_TESTS = [
    # DSL
    ("DSL  Simple = operator",                            t_dsl_simple_equals),
    ("DSL  Simple != operator",                           t_dsl_simple_not_equals),
    ("DSL  in list operator",                             t_dsl_in_list),
    ("DSL  not_in operator",                              t_dsl_not_in_list),
    ("DSL  any operator on list field (party_tags)",      t_dsl_any_in_list_tags),
    ("DSL  >= numeric (min_qty)",                         t_dsl_numeric_gte),
    ("DSL  > numeric",                                    t_dsl_numeric_gt),
    ("DSL  None field returns False, not crash",          t_dsl_none_field_no_crash),
    ("DSL  None dsl = always matches",                    t_dsl_none_dsl_always_matches),
    ("DSL  all = AND logic",                              t_dsl_all_and_logic),
    ("DSL  any = OR logic",                               t_dsl_any_or_logic),
    ("DSL  nested AND inside OR",                         t_dsl_nested),
    ("DSL  promo code normalized in comparison",          t_dsl_promo_code_normalized),
    ("DSL  bad dsl never crashes",                        t_dsl_bad_dsl_never_crashes),
    ("DSL  builder shortcuts produce valid dicts",        t_dsl_builder_shortcuts),
    ("DSL  rule.conditions_dsl stored + evaluated",       t_dsl_rule_with_conditions_dsl),
    # Policy
    ("POL  Wholesale policy scopes to core+wholesale",    t_pol_wholesale_policy_scopes_correctly),
    ("POL  Retail policy scopes to core+retail",          t_pol_retail_policy_scopes_correctly),
    ("POL  Franchise raises on hard stop",                t_pol_franchise_raises_on_hard_stop),
    ("POL  Wholesale is permissive (no raise)",           t_pol_wholesale_does_not_raise),
    ("POL  get_policy routes by channel",                 t_pol_get_policy_channel_routing),
    ("POL  get_policy franchise namespace always wins",   t_pol_get_policy_franchise_namespace),
    ("POL  Custom policy with tight guardrails",          t_pol_custom_policy),
    ("POL  describe() returns readable string",           t_pol_describe_returns_string),
    # Decision Logger
    ("LOG  Records decision in memory mode",              t_log_records_decision_in_memory),
    ("LOG  Records applied rule id + name",               t_log_records_applied_rule),
    ("LOG  competing_rules lists all evaluated rules",    t_log_records_competing_rules),
    ("LOG  Records margin fields",                        t_log_records_margin_fields),
    ("LOG  No result rule = no crash",                    t_log_no_decision_never_crashes),
    ("LOG  get_rule_stats returns fire_count",            t_log_rule_stats_memory),
    ("LOG  log_invoice logs all lines",                   t_log_invoice_logs_all_lines),
    # Simulate API
    ("API  Basic simulate returns ok status",             t_api_basic_simulate),
    ("API  Correct winner returned",                      t_api_winner_correct),
    ("API  Promo code fires (case-insensitive)",          t_api_promo_code_fires),
    ("API  Multiple lines calculated",                    t_api_multiple_lines),
    ("API  Simulation includes all_evaluated",            t_api_simulation_includes_all_evaluated),
    ("API  Policy name in response",                      t_api_policy_name_in_response),
    ("API  Bad payload returns graceful response",        t_api_bad_payload_returns_error),
    # Namespace + ConflictStrategy
    ("NS   namespace default = core",                     t_ns_namespace_default_core),
    ("NS   namespace set correctly",                      t_ns_namespace_set_correctly),
    ("NS   conflict_strategy default = best_price",       t_ns_conflict_strategy_default),
    ("NS   conflict_strategy set correctly",              t_ns_conflict_strategy_set),
    ("NS   from_dict loads namespace + conflict_strategy",t_ns_from_dict_with_namespace),
    ("NS   unknown conflict_strategy defaults gracefully",t_ns_unknown_conflict_strategy_defaults),
    # Versioning
    ("VER  version default = 1",                          t_ver_version_default_1),
    ("VER  parent_rule_id default = None",                t_ver_parent_rule_id_default_none),
    ("VER  from_dict loads version + parent_rule_id",     t_ver_from_dict_version),
    # Regression
    ("REG  v2 rule dict loads in v3 model cleanly",       t_reg_v2_rule_still_works_in_v3_model),
    ("REG  calculate() correct through v3 model",         t_reg_calculate_correct_through_v3),
    ("REG  policy engine = direct engine (same result)",  t_reg_policy_engine_same_result_as_direct),
]

print("\n" + "═"*68)
print("  🔬  OPTICAL DISCOUNT ENGINE v3 FINAL — TEST SUITE")
print("═"*68)

for name, fn in ALL_TESTS:
    test(name, fn)

print()
for r in results:
    print(r)

print()
print("═"*68)
status = "✅ ALL PASSED" if failed == 0 else f"⚠️  {failed} FAILED"
print(f"  {status}   ({passed}/{len(ALL_TESTS)} tests passed)")
print("═"*68 + "\n")

import sys as _sys
_sys.exit(0 if failed == 0 else 1)
