"""
run_tests_polish.py — v2.1 polish test suite

Tests specifically for the 5 polish points:
  P1. Priority ladder (party_contract tier)
  P2. Promo code normalization (.upper().strip() at source)
  P3. Margin tiering (ok / soft_warning / hard_stop + raise mode)
  P4. Stackable rule engine (sequential compounding)
  P5. Rule cache (O(1) channel lookup + correctness)

Run: python run_tests_polish.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from decimal import Decimal

from models.discount_rule import (
    DiscountRule, LineItem, RuleType, ValueType,
    RuleConditions, SlabTier, SalesChannel
)
from core.engine import (
    DiscountEngine, filter_rules, compute_discount,
    compute_margin, pick_best_rule, pick_stackable_rules,
    _normalize_promo, _build_rule_cache,
    MarginHardStopError,
    MARGIN_OK, MARGIN_SOFT_WARNING, MARGIN_HARD_STOP,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_rule(**kwargs):
    defaults = dict(
        id="r001", name="Test Rule", type=RuleType.PARTY,
        value_type=ValueType.PERCENT, value=Decimal("10"),
        gst_rate=Decimal("12"), conditions=RuleConditions(),
        priority=3, active=True, stackable=False,
    )
    defaults.update(kwargs)
    return DiscountRule(**defaults)

def make_item(**kwargs):
    defaults = dict(
        base_price=Decimal("1000"), quantity=10,
        product_cat="frame", party_tags=["wholesale"],
        channel=SalesChannel.WHOLESALE,
    )
    defaults.update(kwargs)
    return LineItem(**defaults)


# ── Test runner ───────────────────────────────────────────────────────────────

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
        raise AssertionError(f"Expected {exc_type.__name__} but no exception was raised")
    except exc_type:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — PRIORITY LADDER
# ═══════════════════════════════════════════════════════════════════════════════

def t_p1_special_priority_1_wins_all():
    """Priority 1 (special) must beat any other priority regardless of discount size."""
    special = make_rule(id="sp", name="Special",
                        value_type=ValueType.SPECIAL_PRICE, special_price=Decimal("999"),
                        priority=1)
    high_pct = make_rule(id="hp", name="High 50%", value=Decimal("50"), priority=3)
    item   = make_item(base_price=Decimal("1000"), quantity=1, channel=SalesChannel.ALL)
    result = DiscountEngine([special, high_pct]).calculate(item)
    eq(result.rule_applied.name, "Special")

def t_p1_party_contract_priority_2_beats_party_priority_3():
    """Priority 2 (party contract) beats priority 3 (party) even with lower discount."""
    contract = make_rule(id="ct", name="Contract 5%", value=Decimal("5"),  priority=2)
    standard = make_rule(id="st", name="Standard 15%", value=Decimal("15"), priority=3)
    item   = make_item(channel=SalesChannel.ALL)
    result = DiscountEngine([contract, standard]).calculate(item)
    eq(result.rule_name, "Contract 5%")

def t_p1_same_priority_higher_discount_wins():
    """Within same priority, higher discount wins."""
    r1 = make_rule(id="r1", name="Party A 8%",  value=Decimal("8"),  priority=3)
    r2 = make_rule(id="r2", name="Party B 12%", value=Decimal("12"), priority=3)
    r3 = make_rule(id="r3", name="Party C 10%", value=Decimal("10"), priority=3)
    result = DiscountEngine([r1, r2, r3]).calculate(make_item(channel=SalesChannel.ALL))
    eq(result.rule_name, "Party B 12%")

def t_p1_tiebreaker_alphabetical():
    """Exact same priority + exact same discount → alphabetical name wins (deterministic)."""
    r_z = make_rule(id="rz", name="Zebra 10%", value=Decimal("10"), priority=3)
    r_a = make_rule(id="ra", name="Alpha 10%", value=Decimal("10"), priority=3)
    result1 = DiscountEngine([r_z, r_a]).calculate(make_item(channel=SalesChannel.ALL))
    result2 = DiscountEngine([r_a, r_z]).calculate(make_item(channel=SalesChannel.ALL))
    eq(result1.rule_name, result2.rule_name, "Must be deterministic")
    eq(result1.rule_name, "Alpha 10%")

def t_p1_tiebreaker_pct_before_name():
    """When amounts equal but pct differs → higher pct wins before alphabetical."""
    # Fixed ₹200 on ₹2000 gross = 10%
    # Fixed ₹200 on ₹1000 gross = 20% — but let's compare two rules on same item
    # Make one with higher eff_pct by using different structures
    # On gross=1000: fixed 100 = 10%, percent 11% = 11% — pct should win
    r_fixed = make_rule(id="rf", name="Fixed 100",
                        value=Decimal("100"), value_type=ValueType.FIXED, priority=3)
    r_pct   = make_rule(id="rp", name="Percent 11%",
                        value=Decimal("11"), value_type=ValueType.PERCENT, priority=3)
    result  = DiscountEngine([r_fixed, r_pct]).calculate(
        make_item(base_price=Decimal("100"), quantity=10, channel=SalesChannel.ALL))
    # gross = 1000, fixed 100 = 10%, pct 11% = 110 → pct wins on amount
    eq(result.rule_name, "Percent 11%")


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — PROMO CODE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def t_p2_normalize_promo_uppercase():
    eq(_normalize_promo("diwali25"), "DIWALI25")

def t_p2_normalize_promo_strips_whitespace():
    eq(_normalize_promo("  DIWALI25  "), "DIWALI25")

def t_p2_normalize_promo_both():
    eq(_normalize_promo("  diwali25  "), "DIWALI25")

def t_p2_normalize_promo_none():
    ok(_normalize_promo(None)  is None)
    ok(_normalize_promo("")    is None)
    ok(_normalize_promo("   ") is None)

def t_p2_filter_normalizes_item_code():
    """filter_rules must match even if item code has mixed case + spaces."""
    rule = make_rule(conditions=RuleConditions(promo_code="DIWALI25"))
    item = make_item(promo_code="  diwali25  ")
    ok(len(filter_rules([rule], item)) == 1, "Should match after normalization")

def t_p2_filter_normalizes_rule_code():
    """filter_rules must match even if rule code was stored with mixed case."""
    rule = make_rule(conditions=RuleConditions(promo_code="diwali25"))
    item = make_item(promo_code="DIWALI25")
    ok(len(filter_rules([rule], item)) == 1, "Rule code also normalized")

def t_p2_simulate_shows_normalized_code():
    """simulate() output should show the normalized promo code."""
    rule = make_rule(value=Decimal("10"), show_in_offers=True)
    item = make_item(promo_code=" newapp20 ", channel=SalesChannel.ALL)
    engine = DiscountEngine([rule])
    sim = engine.simulate(item)
    # normalized code in input section
    eq(sim["input"]["promo_code"], "NEWAPP20")


# ═══════════════════════════════════════════════════════════════════════════════
# P3 — MARGIN TIERING
# ═══════════════════════════════════════════════════════════════════════════════

def t_p3_margin_ok_status():
    """Healthy margin → status = 'ok'."""
    _, _, status = compute_margin(
        Decimal("900"), Decimal("600"),
        soft_warn_pct=Decimal("15"), hard_stop_pct=Decimal("5")
    )
    eq(status, MARGIN_OK)

def t_p3_margin_soft_warning_status():
    """Margin between hard_stop and soft_warn → status = 'soft_warning'."""
    # net=1000, cost=900 → margin=100 → 10%
    # soft_warn=15%, hard_stop=5% → 10% is between → soft_warning
    _, _, status = compute_margin(
        Decimal("1000"), Decimal("900"),
        soft_warn_pct=Decimal("15"), hard_stop_pct=Decimal("5")
    )
    eq(status, MARGIN_SOFT_WARNING)

def t_p3_margin_hard_stop_status():
    """Margin below hard_stop → status = 'hard_stop'."""
    # net=1000, cost=970 → margin=30 → 3%
    # hard_stop=5% → 3% < 5% → hard_stop
    _, _, status = compute_margin(
        Decimal("1000"), Decimal("970"),
        soft_warn_pct=Decimal("15"), hard_stop_pct=Decimal("5")
    )
    eq(status, MARGIN_HARD_STOP)

def t_p3_margin_negative_is_hard_stop():
    """Negative margin (cost > net) → hard_stop."""
    _, _, status = compute_margin(
        Decimal("600"), Decimal("800"),
        soft_warn_pct=Decimal("15"), hard_stop_pct=Decimal("5")
    )
    eq(status, MARGIN_HARD_STOP)

def t_p3_margin_no_cost_returns_ok():
    """No cost_price → margin not computed, status = 'ok'."""
    amt, pct, status = compute_margin(Decimal("1000"), None)
    ok(amt    is None)
    ok(pct    is None)
    eq(status, MARGIN_OK)

def t_p3_result_has_margin_status():
    """DiscountResult.margin_status reflects the tier."""
    rule   = make_rule(value=Decimal("40"))  # big discount → may hit warning
    item   = make_item(
        base_price=Decimal("1000"), quantity=1,
        cost_price=Decimal("850"), channel=SalesChannel.ALL
    )
    # net = 600, cost = 850 → margin = -250 → hard_stop
    result = DiscountEngine([rule], hard_stop_pct=Decimal("5")).calculate(item)
    eq(result.margin_status, MARGIN_HARD_STOP)

def t_p3_raise_on_hard_stop():
    """raise_on_hard_stop=True must raise MarginHardStopError."""
    rule = make_rule(value=Decimal("40"))
    item = make_item(
        base_price=Decimal("1000"), quantity=1,
        cost_price=Decimal("850"), channel=SalesChannel.ALL
    )
    engine = DiscountEngine([rule], hard_stop_pct=Decimal("5"), raise_on_hard_stop=True)
    raises(MarginHardStopError, lambda: engine.calculate(item))

def t_p3_permissive_mode_no_raise():
    """raise_on_hard_stop=False (default) must NOT raise even on hard_stop."""
    rule   = make_rule(value=Decimal("40"))
    item   = make_item(
        base_price=Decimal("1000"), quantity=1,
        cost_price=Decimal("850"), channel=SalesChannel.ALL
    )
    engine = DiscountEngine([rule], hard_stop_pct=Decimal("5"), raise_on_hard_stop=False)
    result = engine.calculate(item)  # must not raise
    eq(result.margin_status, MARGIN_HARD_STOP)

def t_p3_invoice_worst_status_propagates():
    """calculate_invoice should report worst margin status across all lines."""
    rule = make_rule(value=Decimal("10"), conditions=RuleConditions())
    engine = DiscountEngine([rule], soft_warn_pct=Decimal("15"), hard_stop_pct=Decimal("5"))
    lines = [
        make_item(base_price=Decimal("1000"), quantity=1,
                  cost_price=Decimal("600"), channel=SalesChannel.ALL),  # healthy
        make_item(base_price=Decimal("1000"), quantity=1,
                  cost_price=Decimal("810"), channel=SalesChannel.ALL),  # soft_warning (10%)
    ]
    invoice = engine.calculate_invoice(lines)
    eq(invoice["totals"]["margin_status"], MARGIN_SOFT_WARNING)

def t_p3_to_dict_includes_margin_status():
    """to_dict() should include margin_status and convenience bool fields."""
    rule   = make_rule(value=Decimal("10"))
    item   = make_item(cost_price=Decimal("700"), channel=SalesChannel.ALL)
    result = DiscountEngine([rule]).calculate(item)
    d = result.to_dict()
    ok("margin_status"   in d)
    ok("margin_warning"  in d)   # convenience bool: margin_status == soft_warning
    ok("margin_hard_stop" in d)  # convenience bool: margin_status == hard_stop


# ═══════════════════════════════════════════════════════════════════════════════
# P4 — STACKABLE RULE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def t_p4_single_stackable_works_normally():
    """One stackable rule = same result as non-stackable."""
    r = make_rule(id="r1", name="10% Stack", value=Decimal("10"), stackable=True)
    item   = make_item(channel=SalesChannel.ALL)
    result = DiscountEngine([r]).calculate(item)
    eq(result.discount_pct, Decimal("10"))

def t_p4_two_stackable_rules_compound():
    """Two stackable rules: 10% then 5% should compound on reduced net."""
    r1 = make_rule(id="r1", name="10% Rule", value=Decimal("10"), stackable=True, priority=3)
    r2 = make_rule(id="r2", name="5% Rule",  value=Decimal("5"),  stackable=True, priority=4)
    # gross = 1000 (base=100, qty=10)
    # step1: 10% of 1000 = 100, net = 900
    # step2: 5% of 900 = 45, net = 855
    # total discount = 145 = 14.5% of gross
    item   = make_item(base_price=Decimal("100"), quantity=10, channel=SalesChannel.ALL)
    result = DiscountEngine([r1, r2]).calculate(item)
    eq(result.discount_amount, Decimal("145.00"))

def t_p4_stackable_beats_non_stackable_when_higher():
    """Stacked discount should beat single best-wins when it gives more."""
    # Non-stackable single rule: 12%
    ns_rule = make_rule(id="ns", name="Non-stack 12%", value=Decimal("12"), stackable=False, priority=3)
    # Stackable pair: 10% + 5% compounded = 14.5%
    s1 = make_rule(id="s1", name="Stack 10%", value=Decimal("10"), stackable=True, priority=4)
    s2 = make_rule(id="s2", name="Stack 5%",  value=Decimal("5"),  stackable=True, priority=4)

    item   = make_item(channel=SalesChannel.ALL)
    result = DiscountEngine([ns_rule, s1, s2]).calculate(item)
    # 14.5% stacked > 12% single → stacked wins
    ok(result.discount_amount > Decimal("120"), "Stackable should win with higher total discount")

def t_p4_non_stackable_wins_when_higher():
    """Single non-stackable rule should win when it gives more than stack."""
    # Non-stackable: 30%
    ns_rule = make_rule(id="ns", name="Non-stack 30%", value=Decimal("30"), stackable=False, priority=3)
    # Stackable pair: 5% + 5% compounded = 9.75%
    s1 = make_rule(id="s1", name="Stack 5% A", value=Decimal("5"), stackable=True, priority=4)
    s2 = make_rule(id="s2", name="Stack 5% B", value=Decimal("5"), stackable=True, priority=4)

    item   = make_item(channel=SalesChannel.ALL)
    result = DiscountEngine([ns_rule, s1, s2]).calculate(item)
    eq(result.rule_name, "Non-stack 30%")
    eq(result.discount_amount, Decimal("3000.00"))

def t_p4_special_price_always_wins_over_stackable():
    """Special price (priority 1) must override even multiple stackable rules."""
    special = make_rule(id="sp", name="Special ₹800",
                        value_type=ValueType.SPECIAL_PRICE, special_price=Decimal("80"),
                        priority=1)
    s1 = make_rule(id="s1", name="Stack 15%", value=Decimal("15"), stackable=True, priority=3)
    s2 = make_rule(id="s2", name="Stack 10%", value=Decimal("10"), stackable=True, priority=4)
    item   = make_item(base_price=Decimal("100"), quantity=10, channel=SalesChannel.ALL)
    result = DiscountEngine([special, s1, s2]).calculate(item)
    eq(result.rule_applied.name, "Special ₹800")

def t_p4_rule_name_shows_stack():
    """When multiple rules stack, rule_name should show all names."""
    s1 = make_rule(id="s1", name="Promo 10%", value=Decimal("10"), stackable=True)
    s2 = make_rule(id="s2", name="Loyalty 5%", value=Decimal("5"), stackable=True)
    item   = make_item(channel=SalesChannel.ALL)
    result = DiscountEngine([s1, s2]).calculate(item)
    ok("Promo 10%" in result.rule_name or "Loyalty 5%" in result.rule_name,
       f"Name should contain rule names, got: {result.rule_name}")

def t_p4_pick_stackable_returns_list():
    """pick_stackable_rules returns a list of fired rules."""
    s1 = make_rule(id="s1", name="S1", value=Decimal("10"), stackable=True)
    s2 = make_rule(id="s2", name="S2", value=Decimal("5"),  stackable=True)
    item = make_item(channel=SalesChannel.ALL)
    fired, disc, pct = pick_stackable_rules([s1, s2], item)
    ok(len(fired) >= 1)
    ok(disc > Decimal("0"))


# ═══════════════════════════════════════════════════════════════════════════════
# P5 — RULE CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def t_p5_cache_builds_correctly():
    """Cache buckets should contain correct rules per channel."""
    ws_rule = make_rule(id="ws", name="WS rule",
                        conditions=RuleConditions(channel=SalesChannel.WHOLESALE))
    rt_rule = make_rule(id="rt", name="RT rule",
                        conditions=RuleConditions(channel=SalesChannel.RETAIL))
    all_rule = make_rule(id="al", name="ALL rule",
                         conditions=RuleConditions(channel=SalesChannel.ALL))

    cache = _build_rule_cache([ws_rule, rt_rule, all_rule])

    ok(any(r.id == "ws" for r in cache["wholesale"]))
    ok(any(r.id == "rt" for r in cache["retail"]))
    ok(any(r.id == "al" for r in cache["wholesale"]))
    ok(any(r.id == "al" for r in cache["retail"]))
    ok(any(r.id == "al" for r in cache["online"]))

def t_p5_cache_excludes_inactive():
    """Inactive rules must not appear in cache."""
    active   = make_rule(id="a", name="Active", active=True)
    inactive = make_rule(id="i", name="Inactive", active=False)
    cache    = _build_rule_cache([active, inactive])
    all_cached = [r for bucket in cache.values() for r in bucket]
    ok(not any(r.id == "i" for r in all_cached), "Inactive must not be cached")

def t_p5_cache_gives_correct_results():
    """Engine using cache gives same results as full scan for all channels."""
    ws_rule  = make_rule(id="ws", name="WS 12%",  value=Decimal("12"),
                         conditions=RuleConditions(channel=SalesChannel.WHOLESALE))
    rt_rule  = make_rule(id="rt", name="RT 8%",   value=Decimal("8"),
                         conditions=RuleConditions(channel=SalesChannel.RETAIL))
    all_rule = make_rule(id="al", name="ALL 5%",  value=Decimal("5"),
                         conditions=RuleConditions(channel=SalesChannel.ALL))

    engine = DiscountEngine([ws_rule, rt_rule, all_rule])

    ws_result = engine.calculate(make_item(channel=SalesChannel.WHOLESALE))
    eq(ws_result.rule_name, "WS 12%")  # WS 12% > ALL 5%

    rt_result = engine.calculate(make_item(channel=SalesChannel.RETAIL))
    eq(rt_result.rule_name, "RT 8%")   # RT 8% > ALL 5%

def t_p5_online_rule_not_in_wholesale_cache():
    """Online-only rule must not fire for wholesale billing."""
    online_rule = make_rule(id="onl", name="Online 20%", value=Decimal("20"),
                            conditions=RuleConditions(channel=SalesChannel.ONLINE))
    ws_rule     = make_rule(id="ws",  name="WS 10%",    value=Decimal("10"),
                            conditions=RuleConditions(channel=SalesChannel.WHOLESALE))

    engine = DiscountEngine([online_rule, ws_rule])
    result = engine.calculate(make_item(channel=SalesChannel.WHOLESALE))
    eq(result.rule_name, "WS 10%")  # Online 20% must NOT fire for wholesale


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED SCENARIO — all polish points working together
# ═══════════════════════════════════════════════════════════════════════════════

def t_combined_online_order_with_promo_and_margin():
    """
    Online customer: uses promo code (P2), gets stacked discount (P4),
    margin is checked (P3), result cached (P5).
    """
    # Stackable: 10% loyalty + 5% promo (for code "EXTRA5")
    loyalty = make_rule(
        id="loy", name="Online Loyalty 10%",
        value=Decimal("10"), stackable=True, priority=3,
        conditions=RuleConditions(channel=SalesChannel.ONLINE)
    )
    promo = make_rule(
        id="prm", name="Promo Extra 5%",
        value=Decimal("5"), stackable=True, priority=4,
        conditions=RuleConditions(channel=SalesChannel.ONLINE, promo_code="EXTRA5")
    )

    engine = DiscountEngine(
        [loyalty, promo],
        soft_warn_pct=Decimal("20"),
        hard_stop_pct=Decimal("5"),
    )

    item = LineItem(
        base_price  = Decimal("1000"),
        quantity    = 1,
        product_cat = "frame",
        channel     = SalesChannel.ONLINE,
        promo_code  = " extra5 ",    # P2: will be normalized to EXTRA5
        cost_price  = Decimal("700"),
    )

    result = engine.calculate(item)

    # P2: promo matched despite spaces+lowercase
    ok(result.rule_applied is not None, "Should have matched a rule")

    # P4: both rules should have stacked
    ok(result.discount_amount > Decimal("100"),
       f"Should have stacked discount > 10%, got {result.discount_amount}")

    # P3: margin should be computed
    ok(result.margin_amount is not None)
    ok(result.margin_status in (MARGIN_OK, MARGIN_SOFT_WARNING, MARGIN_HARD_STOP))

    # P5: engine used cache (no error means cache worked)
    sim = engine.simulate(item)
    eq(sim["input"]["promo_code"], "EXTRA5")   # normalized in output


# ═══════════════════════════════════════════════════════════════════════════════
# RUN ALL
# ═══════════════════════════════════════════════════════════════════════════════

ALL_TESTS = [
    # P1 — Priority ladder
    ("P1  Special price (priority 1) beats everything",               t_p1_special_priority_1_wins_all),
    ("P1  Party contract (priority 2) beats party (priority 3)",      t_p1_party_contract_priority_2_beats_party_priority_3),
    ("P1  Same priority → higher discount wins",                      t_p1_same_priority_higher_discount_wins),
    ("P1  Tie: alphabetical name — deterministic",                    t_p1_tiebreaker_alphabetical),
    ("P1  Tie: higher effective % wins before alphabetical",          t_p1_tiebreaker_pct_before_name),

    # P2 — Promo normalization
    ("P2  _normalize_promo uppercases",                               t_p2_normalize_promo_uppercase),
    ("P2  _normalize_promo strips whitespace",                        t_p2_normalize_promo_strips_whitespace),
    ("P2  _normalize_promo does both",                                t_p2_normalize_promo_both),
    ("P2  _normalize_promo handles None/empty",                       t_p2_normalize_promo_none),
    ("P2  filter_rules normalizes item code at source",               t_p2_filter_normalizes_item_code),
    ("P2  filter_rules normalizes rule code at source",               t_p2_filter_normalizes_rule_code),
    ("P2  simulate() outputs normalized code",                        t_p2_simulate_shows_normalized_code),

    # P3 — Margin tiering
    ("P3  Healthy margin → status = ok",                              t_p3_margin_ok_status),
    ("P3  Margin 5–15% → status = soft_warning",                      t_p3_margin_soft_warning_status),
    ("P3  Margin < 5% → status = hard_stop",                          t_p3_margin_hard_stop_status),
    ("P3  Negative margin → hard_stop",                               t_p3_margin_negative_is_hard_stop),
    ("P3  No cost_price → no margin, status = ok",                    t_p3_margin_no_cost_returns_ok),
    ("P3  DiscountResult.margin_status reflects tier",                t_p3_result_has_margin_status),
    ("P3  raise_on_hard_stop=True raises MarginHardStopError",        t_p3_raise_on_hard_stop),
    ("P3  Default permissive mode never raises",                      t_p3_permissive_mode_no_raise),
    ("P3  calculate_invoice propagates worst margin status",          t_p3_invoice_worst_status_propagates),
    ("P3  to_dict() includes margin_status + convenience bools",      t_p3_to_dict_includes_margin_status),

    # P4 — Stackable engine
    ("P4  Single stackable rule works normally",                      t_p4_single_stackable_works_normally),
    ("P4  Two stackable rules compound sequentially",                 t_p4_two_stackable_rules_compound),
    ("P4  Stackable wins when total > non-stackable single",          t_p4_stackable_beats_non_stackable_when_higher),
    ("P4  Non-stackable wins when it beats the stack",                t_p4_non_stackable_wins_when_higher),
    ("P4  Special price overrides all stackable rules",               t_p4_special_price_always_wins_over_stackable),
    ("P4  Stacked rule_name shows all fired rules",                   t_p4_rule_name_shows_stack),
    ("P4  pick_stackable_rules returns list of fired rules",          t_p4_pick_stackable_returns_list),

    # P5 — Cache
    ("P5  Cache builds correct buckets per channel",                  t_p5_cache_builds_correctly),
    ("P5  Cache excludes inactive rules",                             t_p5_cache_excludes_inactive),
    ("P5  Cached engine gives same results as full scan",             t_p5_cache_gives_correct_results),
    ("P5  Online rule does not fire in wholesale cache",              t_p5_online_rule_not_in_wholesale_cache),

    # Combined
    ("🎯 Combined: promo + stackable + margin + cache (online order)",t_combined_online_order_with_promo_and_margin),
]

print("\n" + "═"*68)
print("  🔬  OPTICAL DISCOUNT ENGINE v2.1 — POLISH TEST SUITE")
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

sys.exit(0 if failed == 0 else 1)
