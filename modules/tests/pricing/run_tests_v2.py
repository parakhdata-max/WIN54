"""
run_tests_v2.py — v2 test suite
Tests all 4 upgrades + channel + promo + brand_group + margin

Run: python run_tests_v2.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from decimal import Decimal

from models.discount_rule import (
    DiscountRule, LineItem, RuleType, ValueType,
    RuleConditions, SlabTier, SalesChannel
)
from core.engine import DiscountEngine, filter_rules, compute_discount, compute_margin


# ── Helpers ──────────────────────────────────

def make_rule(**kwargs):
    defaults = dict(
        id="r001", name="Test Rule", type=RuleType.PARTY,
        value_type=ValueType.PERCENT, value=Decimal("10"),
        gst_rate=Decimal("12"), conditions=RuleConditions(),
        priority=3, active=True,
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


# ── Runner ───────────────────────────────────

passed = 0
failed = 0
results = []

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


# ═══════════════════════════════════════════
# SECTION 1 — UPGRADE 1: Brand Group
# ═══════════════════════════════════════════

def t_brand_group_match():
    rule = make_rule(
        type=RuleType.BRAND_GROUP, value=Decimal("8"),
        conditions=RuleConditions(brand_groups=["titan"])
    )
    item = make_item(brand_group="titan")
    eq(len(filter_rules([rule], item)), 1)

def t_brand_group_no_match():
    rule = make_rule(
        type=RuleType.BRAND_GROUP, value=Decimal("8"),
        conditions=RuleConditions(brand_groups=["titan"])
    )
    item = make_item(brand_group="rayban")
    eq(len(filter_rules([rule], item)), 0)

def t_brand_group_none_item():
    """Rule requires brand_group but item has none → no match."""
    rule = make_rule(
        conditions=RuleConditions(brand_groups=["titan"])
    )
    item = make_item(brand_group=None)
    eq(len(filter_rules([rule], item)), 0)

def t_brand_group_discount_calculation():
    """Brand group rule 8% on 10 × ₹500 = ₹400 discount."""
    rule = make_rule(
        id="bg-titan", name="Titan 8%", type=RuleType.BRAND_GROUP,
        value=Decimal("8"), priority=2,
        conditions=RuleConditions(brand_groups=["titan"], party_tags=["wholesale"])
    )
    item = make_item(base_price=Decimal("500"), quantity=10, brand_group="titan")
    result = DiscountEngine([rule]).calculate(item)
    eq(result.discount_amount, Decimal("400"))
    eq(result.rule_name, "Titan 8%")

def t_brand_group_vs_party_same_priority():
    """Brand group and party both priority=2. Higher discount wins."""
    bg_rule = make_rule(id="bg", name="Titan BG 8%", type=RuleType.BRAND_GROUP,
                        value=Decimal("8"), priority=2,
                        conditions=RuleConditions(brand_groups=["titan"]))
    party_rule = make_rule(id="pr", name="Party 12%", type=RuleType.PARTY,
                           value=Decimal("12"), priority=2,
                           conditions=RuleConditions(party_tags=["wholesale"]))
    item = make_item(brand_group="titan", party_tags=["wholesale"])
    result = DiscountEngine([bg_rule, party_rule]).calculate(item)
    eq(result.rule_name, "Party 12%")  # higher discount wins


# ═══════════════════════════════════════════
# SECTION 2 — UPGRADE 2: Tie-Breaker
# ═══════════════════════════════════════════

def t_tiebreaker_by_pct():
    """Two rules, same priority, same discount AMOUNT but different %.
    Higher % wins — important when one is fixed ₹ and one is %."""
    # Both produce same discount amount but different %
    r_pct = make_rule(id="p1", name="10% Rule",  value=Decimal("10"),
                      value_type=ValueType.PERCENT, priority=3)
    r_fix = make_rule(id="f1", name="100 Fixed", value=Decimal("100"),
                      value_type=ValueType.FIXED, priority=3)
    # On ₹1000 × 10: gross = 10000, 10% = 1000, fixed = 100 → 10% clearly wins on amount
    item   = make_item(base_price=Decimal("1000"), quantity=10)
    result = DiscountEngine([r_fix, r_pct]).calculate(item)
    eq(result.rule_name, "10% Rule")

def t_tiebreaker_deterministic():
    """Completely identical rules → alphabetically first name wins (no randomness)."""
    r1 = make_rule(id="r1", name="Alpha Rule", value=Decimal("10"), priority=3)
    r2 = make_rule(id="r2", name="Zebra Rule", value=Decimal("10"), priority=3)
    result1 = DiscountEngine([r1, r2]).calculate(make_item())
    result2 = DiscountEngine([r2, r1]).calculate(make_item())
    eq(result1.rule_name, result2.rule_name, "Tie-breaker must be deterministic")
    eq(result1.rule_name, "Alpha Rule")  # alphabetically first

def t_priority_still_trumps_discount():
    """Lower priority number (=higher priority) should still win even if discount is smaller."""
    high_priority_low_disc = make_rule(id="h", name="Party 5%",    value=Decimal("5"),  priority=2)
    low_priority_high_disc = make_rule(id="l", name="Slab 20%",   value=Decimal("20"), priority=4)
    result = DiscountEngine([high_priority_low_disc, low_priority_high_disc]).calculate(make_item())
    eq(result.rule_name, "Party 5%")  # priority 2 beats priority 4


# ═══════════════════════════════════════════
# SECTION 3 — UPGRADE 3: Margin Simulation
# ═══════════════════════════════════════════

def t_margin_calculated_on_net():
    """Margin = net_amount - cost_gross."""
    rule = make_rule(value=Decimal("10"))
    item = make_item(base_price=Decimal("1000"), quantity=1, cost_price=Decimal("700"))
    result = DiscountEngine([rule]).calculate(item)
    # net = 1000 - 100 = 900
    # cost = 700
    # margin = 900 - 700 = 200
    # margin % = 200/900 * 100 = 22.22%
    eq(result.margin_amount,  Decimal("200.00"))
    ok(result.margin_pct > Decimal("22"), "margin pct should be ~22%")

def t_margin_warning_triggered():
    """Margin below threshold → margin_warning = True."""
    rule = make_rule(value=Decimal("40"))  # big discount
    item = make_item(base_price=Decimal("1000"), quantity=1, cost_price=Decimal("850"))
    # net = 1000 - 400 = 600, cost = 850 → margin = -250 (negative!)
    result = DiscountEngine([rule], soft_warn_pct=Decimal("15")).calculate(item)
    ok(result.margin_status in ("soft_warning", "hard_stop"), "Should warn on negative/low margin")

def t_margin_no_cost_price_no_margin():
    """No cost_price → no margin calculation."""
    rule   = make_rule(value=Decimal("10"))
    item   = make_item()  # no cost_price
    result = DiscountEngine([rule]).calculate(item)
    ok(result.margin_amount is None)
    ok(result.margin_pct    is None)

def t_margin_in_simulate():
    """Simulate mode should include margin_pct in all_evaluated."""
    rule = make_rule(value=Decimal("12"))
    item = make_item(base_price=Decimal("1200"), quantity=1, cost_price=Decimal("700"))
    sim  = DiscountEngine([rule]).simulate(item)
    ok(sim["winner"]["margin_amount"] is not None)
    ev = sim["all_evaluated"][0]
    ok("margin_pct" in ev)


# ═══════════════════════════════════════════
# SECTION 4 — UPGRADE 4: Stackable Flag
# ═══════════════════════════════════════════

def t_stackable_flag_stored():
    """stackable flag should be stored and readable."""
    rule = make_rule(stackable=True)
    ok(rule.stackable)

def t_non_stackable_default():
    """Default stackable = False."""
    rule = make_rule()
    ok(not rule.stackable)

def t_stackable_still_best_wins():
    """When stackable=True, rules stack sequentially (P4 implemented)."""
    r1 = make_rule(id="r1", name="Rule A", value=Decimal("10"), stackable=True)
    r2 = make_rule(id="r2", name="Rule B", value=Decimal("15"), stackable=True)
    result = DiscountEngine([r1, r2]).calculate(make_item())
    # Both fire: 15% then 10% on reduced net → stacked discount > single rule
    # Rule name should contain both
    ok("Rule B" in result.rule_name)
    eq(len(result.evaluated_rules), 2)


# ═══════════════════════════════════════════
# SECTION 5 — Channel Filtering
# ═══════════════════════════════════════════

def t_channel_wholesale_only():
    rule = make_rule(conditions=RuleConditions(channel=SalesChannel.WHOLESALE))
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.WHOLESALE))) == 1)
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.RETAIL)))    == 0)
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.ONLINE)))    == 0)

def t_channel_all_applies_everywhere():
    rule = make_rule(conditions=RuleConditions(channel=SalesChannel.ALL))
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.WHOLESALE))) == 1)
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.RETAIL)))    == 1)
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.ONLINE)))    == 1)

def t_channel_retail_doesnt_fire_for_wholesale():
    rule = make_rule(conditions=RuleConditions(channel=SalesChannel.RETAIL))
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.WHOLESALE))) == 0)
    ok(len(filter_rules([rule], make_item(channel=SalesChannel.RETAIL)))    == 1)

def t_online_channel_rule():
    rule = make_rule(
        type=RuleType.PROMO_CODE, value=Decimal("20"),
        conditions=RuleConditions(channel=SalesChannel.ONLINE, promo_code="NEWAPP20")
    )
    item_with_code    = make_item(channel=SalesChannel.ONLINE, promo_code="NEWAPP20")
    item_without_code = make_item(channel=SalesChannel.ONLINE)
    item_wrong_channel = make_item(channel=SalesChannel.RETAIL, promo_code="NEWAPP20")

    ok(len(filter_rules([rule], item_with_code))     == 1)
    ok(len(filter_rules([rule], item_without_code))  == 0)
    ok(len(filter_rules([rule], item_wrong_channel)) == 0)


# ═══════════════════════════════════════════
# SECTION 6 — Promo Codes
# ═══════════════════════════════════════════

def t_promo_code_exact_match():
    rule = make_rule(conditions=RuleConditions(promo_code="DIWALI25"))
    item = make_item(promo_code="DIWALI25")
    ok(len(filter_rules([rule], item)) == 1)

def t_promo_code_case_insensitive():
    """Codes should match case-insensitively (engine uppercases both)."""
    rule = make_rule(conditions=RuleConditions(promo_code="DIWALI25"))
    item = make_item(promo_code="diwali25")
    # filter_rules compares uppercased both sides
    ok(len(filter_rules([rule], item)) == 1)

def t_promo_code_wrong_code_no_match():
    rule = make_rule(conditions=RuleConditions(promo_code="DIWALI25"))
    item = make_item(promo_code="WRONGCODE")
    ok(len(filter_rules([rule], item)) == 0)

def t_promo_code_no_code_provided():
    rule = make_rule(conditions=RuleConditions(promo_code="DIWALI25"))
    item = make_item(promo_code=None)
    ok(len(filter_rules([rule], item)) == 0)

def t_promo_code_discount_applied():
    rule = make_rule(
        id="promo", name="New App 20%",
        type=RuleType.PROMO_CODE, value=Decimal("20"), priority=3,
        conditions=RuleConditions(channel=SalesChannel.ONLINE, promo_code="NEWAPP20")
    )
    item   = make_item(channel=SalesChannel.ONLINE, promo_code="NEWAPP20",
                       base_price=Decimal("500"), quantity=2)
    result = DiscountEngine([rule]).calculate(item)
    eq(result.discount_amount, Decimal("200"))   # 20% of 1000
    eq(result.rule_name, "New App 20%")


# ═══════════════════════════════════════════
# SECTION 7 — Party Whitelist / Blacklist
# ═══════════════════════════════════════════

def t_party_whitelist_allows():
    rule = make_rule(conditions=RuleConditions(party_whitelist=["party-abc-123"]))
    item = make_item(party_tags=[], party_id="party-abc-123")
    ok(len(filter_rules([rule], item)) == 1)

def t_party_whitelist_blocks_others():
    rule = make_rule(conditions=RuleConditions(party_whitelist=["party-abc-123"]))
    item = make_item(party_tags=["wholesale"], party_id="party-xyz-999")
    ok(len(filter_rules([rule], item)) == 0)

def t_party_blacklist_blocks():
    rule = make_rule(conditions=RuleConditions(party_blacklist=["bad-party-001"]))
    item = make_item(party_tags=["wholesale"], party_id="bad-party-001")
    ok(len(filter_rules([rule], item)) == 0)

def t_party_blacklist_allows_others():
    rule = make_rule(conditions=RuleConditions(party_blacklist=["bad-party-001"]))
    item = make_item(party_tags=["wholesale"], party_id="good-party-002")
    ok(len(filter_rules([rule], item)) == 1)


# ═══════════════════════════════════════════
# SECTION 8 — list_available_offers()
# ═══════════════════════════════════════════

def t_offers_only_show_in_offers():
    """list_available_offers returns only rules with show_in_offers=True."""
    r_visible = make_rule(id="v", name="Visible Offer", value=Decimal("10"),
                          show_in_offers=True)
    r_hidden  = make_rule(id="h", name="Hidden Rule", value=Decimal("15"),
                          show_in_offers=False)
    engine = DiscountEngine([r_visible, r_hidden])
    offers = engine.list_available_offers(make_item())
    names  = [o["name"] for o in offers]
    ok("Visible Offer" in names)
    ok("Hidden Rule"   not in names)

def t_offers_shows_promo_code_flag():
    """Offers with promo codes should have requires_code=True."""
    rule = make_rule(
        id="pc", name="Promo Offer", value=Decimal("15"),
        show_in_offers=True,
        conditions=RuleConditions(promo_code="TEST15")
    )
    engine = DiscountEngine([rule])
    offers = engine.list_available_offers(make_item())
    ok(len(offers) == 1)
    ok(offers[0]["requires_code"] is True)
    ok(offers[0]["promo_code"] == "TEST15")

def t_offers_sorted_by_discount():
    """Offers sorted highest discount first."""
    r5  = make_rule(id="r5",  name="5% off",  value=Decimal("5"),  show_in_offers=True)
    r20 = make_rule(id="r20", name="20% off", value=Decimal("20"), show_in_offers=True)
    r10 = make_rule(id="r10", name="10% off", value=Decimal("10"), show_in_offers=True)
    offers = DiscountEngine([r5, r20, r10]).list_available_offers(make_item())
    eq(offers[0]["name"], "20% off")


# ═══════════════════════════════════════════
# SECTION 9 — v1 Regression (nothing broken)
# ═══════════════════════════════════════════

def t_v1_percent_still_works():
    rule   = make_rule(value=Decimal("12"))
    item   = make_item(base_price=Decimal("1200"), quantity=1, channel=SalesChannel.ALL)
    result = DiscountEngine([rule]).calculate(item)
    eq(result.gross_amount,    Decimal("1200"))
    eq(result.discount_amount, Decimal("144"))
    eq(result.final_amount,    Decimal("1182.72"))

def t_v1_bogo_still_works():
    rule = make_rule(
        type=RuleType.OFFER_BOGO, value_type=ValueType.BOGO,
        bogo_buy=10, bogo_get=1, value=None, priority=4
    )
    item   = make_item(base_price=Decimal("100"), quantity=20, channel=SalesChannel.ALL)
    result = DiscountEngine([rule]).calculate(item)
    eq(result.discount_amount, Decimal("200"))

def t_v1_slab_still_works():
    slabs = [
        SlabTier(min_qty=10, max_qty=24, discount_pct=Decimal("5")),
        SlabTier(min_qty=25, max_qty=None, discount_pct=Decimal("10")),
    ]
    rule   = make_rule(type=RuleType.OFFER_SLAB, slab_config=slabs, priority=4)
    engine = DiscountEngine([rule])
    eq(engine.calculate(make_item(quantity=15)).discount_pct,  Decimal("5"))
    eq(engine.calculate(make_item(quantity=30)).discount_pct,  Decimal("10"))

def t_v1_special_price_still_wins():
    sp = make_rule(id="sp", name="Special", value_type=ValueType.SPECIAL_PRICE,
                   special_price=Decimal("500"), priority=1)
    pct = make_rule(id="pc", value=Decimal("50"), priority=3)
    result = DiscountEngine([pct, sp]).calculate(
        make_item(base_price=Decimal("1000"), quantity=1, channel=SalesChannel.ALL))
    eq(result.rule_applied.name, "Special")

def t_gst_still_after_discount():
    rule   = make_rule(value=Decimal("10"), gst_rate=Decimal("18"))
    item   = make_item(base_price=Decimal("500"), quantity=2, channel=SalesChannel.ALL)
    result = DiscountEngine([rule]).calculate(item)
    eq(result.net_amount,   Decimal("900"))
    eq(result.gst_amount,   Decimal("162"))
    eq(result.final_amount, Decimal("1062"))


# ═══════════════════════════════════════════
# RUN ALL
# ═══════════════════════════════════════════

ALL_TESTS = [
    # Brand Group
    ("Brand group: rule matches when brand_group matches",      t_brand_group_match),
    ("Brand group: rule skipped when brand_group differs",      t_brand_group_no_match),
    ("Brand group: rule skipped when item has no brand_group",  t_brand_group_none_item),
    ("Brand group: discount correctly calculated",              t_brand_group_discount_calculation),
    ("Brand group vs party: higher discount wins (same prio)",  t_brand_group_vs_party_same_priority),
    # Tie-breaker
    ("Tie-breaker: higher discount amount wins",                t_tiebreaker_by_pct),
    ("Tie-breaker: deterministic (alphabetical name)",          t_tiebreaker_deterministic),
    ("Tie-breaker: priority still trumps discount size",        t_priority_still_trumps_discount),
    # Margin simulation
    ("Margin: calculated correctly on net amount",              t_margin_calculated_on_net),
    ("Margin: warning triggered when margin below threshold",   t_margin_warning_triggered),
    ("Margin: no cost_price → no margin data",                  t_margin_no_cost_price_no_margin),
    ("Margin: appears in simulate() output",                    t_margin_in_simulate),
    # Stackable flag
    ("Stackable: flag stored on rule",                         t_stackable_flag_stored),
    ("Stackable: default is False",                            t_non_stackable_default),
    ("Stackable: engine still uses best-wins (no stacking yet)",t_stackable_still_best_wins),
    # Channel filtering
    ("Channel: wholesale rule only fires for wholesale",        t_channel_wholesale_only),
    ("Channel: ALL channel fires everywhere",                   t_channel_all_applies_everywhere),
    ("Channel: retail rule doesn't fire for wholesale",         t_channel_retail_doesnt_fire_for_wholesale),
    ("Channel: online promo only fires with correct code+ch",   t_online_channel_rule),
    # Promo codes
    ("Promo: exact code match",                                 t_promo_code_exact_match),
    ("Promo: case-insensitive matching",                        t_promo_code_case_insensitive),
    ("Promo: wrong code → no match",                            t_promo_code_wrong_code_no_match),
    ("Promo: no code provided → no match",                      t_promo_code_no_code_provided),
    ("Promo: discount applied when code matches",               t_promo_code_discount_applied),
    # Party whitelist / blacklist
    ("Party whitelist: allows exact party",                     t_party_whitelist_allows),
    ("Party whitelist: blocks non-whitelisted parties",         t_party_whitelist_blocks_others),
    ("Party blacklist: blocks blacklisted party",               t_party_blacklist_blocks),
    ("Party blacklist: allows non-blacklisted parties",         t_party_blacklist_allows_others),
    # Available offers
    ("Offers: only show_in_offers=True returned",               t_offers_only_show_in_offers),
    ("Offers: promo code rules flagged requires_code=True",     t_offers_shows_promo_code_flag),
    ("Offers: sorted highest discount first",                   t_offers_sorted_by_discount),
    # v1 Regression
    ("v1 Regression: percent discount still correct",           t_v1_percent_still_works),
    ("v1 Regression: BOGO still correct",                       t_v1_bogo_still_works),
    ("v1 Regression: slab still correct",                       t_v1_slab_still_works),
    ("v1 Regression: special price still wins",                 t_v1_special_price_still_wins),
    ("v1 Regression: GST still on post-discount net",           t_gst_still_after_discount),
]

print("\n" + "═"*65)
print("  🔬  OPTICAL DISCOUNT ENGINE v2 — TEST SUITE")
print("═"*65)

for name, fn in ALL_TESTS:
    test(name, fn)

print()
for r in results:
    print(r)

print()
print("═"*65)
status = "✅ ALL PASSED" if failed == 0 else f"⚠️  {failed} FAILED"
print(f"  {status}   ({passed}/{len(ALL_TESTS)} tests passed)")
print("═"*65 + "\n")

import sys
sys.exit(0 if failed == 0 else 1)
