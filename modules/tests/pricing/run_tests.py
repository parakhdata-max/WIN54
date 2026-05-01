"""
run_tests.py — Runs all test cases without pytest dependency.
Usage: python run_tests.py
"""

import sys, os, traceback
from decimal import Decimal

# Add parent to path
sys.path.insert(0, os.path.dirname(__file__))

from models.discount_rule import (
    DiscountRule, LineItem, RuleType, ValueType, RuleConditions, SlabTier
)
from core.engine import DiscountEngine, filter_rules, compute_discount


# ── Helpers ──────────────────────────────────

def make_rule(**kwargs):
    defaults = dict(
        id="test-001", name="Test Rule", type=RuleType.PARTY,
        value_type=ValueType.PERCENT, value=Decimal("10"),
        gst_rate=Decimal("12"), conditions=RuleConditions(),
        priority=3, active=True,
    )
    defaults.update(kwargs)
    return DiscountRule(**defaults)

def make_item(**kwargs):
    defaults = dict(base_price=Decimal("1000"), quantity=10, product_cat="frame", party_tags=["wholesale"])
    defaults.update(kwargs)
    return LineItem(**defaults)


# ── Test Runner ──────────────────────────────

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

def assert_eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"Expected {b!r}, got {a!r}. {msg}")

def assert_true(val, msg=""):
    if not val:
        raise AssertionError(f"Expected True. {msg}")

# ── TEST CASES ───────────────────────────────

def t_percent_discount():
    rule   = make_rule(value=Decimal("12"), value_type=ValueType.PERCENT)
    item   = make_item(base_price=Decimal("1200"), quantity=1)
    result = DiscountEngine([rule]).calculate(item)
    assert_eq(result.gross_amount,    Decimal("1200"))
    assert_eq(result.discount_amount, Decimal("144"))
    assert_eq(result.net_amount,      Decimal("1056"))
    assert_eq(result.gst_amount,      Decimal("126.72"))
    assert_eq(result.final_amount,    Decimal("1182.72"))

def t_gst_after_discount():
    rule   = make_rule(value=Decimal("10"), value_type=ValueType.PERCENT, gst_rate=Decimal("18"))
    item   = make_item(base_price=Decimal("500"), quantity=2)
    result = DiscountEngine([rule]).calculate(item)
    assert_eq(result.net_amount,   Decimal("900"))
    assert_eq(result.gst_amount,   Decimal("162"))
    assert_eq(result.final_amount, Decimal("1062"))

def t_fixed_discount():
    rule   = make_rule(value=Decimal("50"), value_type=ValueType.FIXED)
    item   = make_item(base_price=Decimal("200"), quantity=1)
    result = DiscountEngine([rule]).calculate(item)
    assert_eq(result.discount_amount, Decimal("50"))
    assert_eq(result.net_amount,      Decimal("150"))

def t_special_price():
    rule = make_rule(
        value_type=ValueType.SPECIAL_PRICE, special_price=Decimal("900"), priority=1
    )
    item   = make_item(base_price=Decimal("1200"), quantity=5)
    result = DiscountEngine([rule]).calculate(item)
    assert_eq(result.discount_amount, Decimal("1500"))
    assert_eq(result.net_amount,      Decimal("4500"))

def t_no_discount():
    result = DiscountEngine([]).calculate(make_item())
    assert_eq(result.discount_amount, Decimal("0"))
    assert_eq(result.rule_name, "No Discount")

def t_bogo():
    rule = make_rule(
        type=RuleType.OFFER_BOGO, value_type=ValueType.BOGO,
        bogo_buy=10, bogo_get=1, value=None, priority=4
    )
    item   = make_item(base_price=Decimal("100"), quantity=20)
    result = DiscountEngine([rule]).calculate(item)
    assert_eq(result.discount_amount, Decimal("200"))

def t_slab():
    slabs = [
        SlabTier(min_qty=10, max_qty=24, discount_pct=Decimal("5")),
        SlabTier(min_qty=25, max_qty=49, discount_pct=Decimal("10")),
        SlabTier(min_qty=50, max_qty=None, discount_pct=Decimal("15")),
    ]
    rule = make_rule(type=RuleType.OFFER_SLAB, slab_config=slabs, priority=4)
    e = DiscountEngine([rule])
    assert_eq(e.calculate(make_item(quantity=15)).discount_pct,  Decimal("5"))
    assert_eq(e.calculate(make_item(quantity=30)).discount_pct,  Decimal("10"))
    assert_eq(e.calculate(make_item(quantity=100)).discount_pct, Decimal("15"))
    assert_eq(e.calculate(make_item(quantity=5)).discount_amount, Decimal("0"))

def t_special_beats_all():
    special = make_rule(id="sp", name="Special Price", value_type=ValueType.SPECIAL_PRICE,
                        special_price=Decimal("500"), priority=1)
    pct     = make_rule(id="pc", name="High 50%", value=Decimal("50"), priority=3)
    result  = DiscountEngine([pct, special]).calculate(make_item(base_price=Decimal("1000"), quantity=1))
    assert_eq(result.rule_applied.name, "Special Price")

def t_best_discount_wins_same_priority():
    r15 = make_rule(id="r1", name="15% off", value=Decimal("15"), priority=3)
    r20 = make_rule(id="r2", name="20% off", value=Decimal("20"), priority=3)
    result = DiscountEngine([r15, r20]).calculate(make_item())
    assert_eq(result.rule_name, "20% off")

def t_only_one_rule_fires():
    rules  = [
        make_rule(id="r1", name="5% off",  value=Decimal("5"),  priority=3),
        make_rule(id="r2", name="12% off", value=Decimal("12"), priority=3),
        make_rule(id="r3", name="8% off",  value=Decimal("8"),  priority=3),
    ]
    result = DiscountEngine(rules).calculate(make_item())
    assert_eq(result.rule_name, "12% off")
    assert_eq(len(result.evaluated_rules), 3)

def t_party_tag_filter():
    rule_w = make_rule(conditions=RuleConditions(party_tags=["wholesale"]))
    rule_v = make_rule(conditions=RuleConditions(party_tags=["vip"]))
    item   = make_item(party_tags=["wholesale"])
    assert_eq(len(filter_rules([rule_w], item)), 1)
    assert_eq(len(filter_rules([rule_v], item)), 0)

def t_product_cat_filter():
    rule = make_rule(conditions=RuleConditions(product_cats=["frame"]))
    assert_eq(len(filter_rules([rule], make_item(product_cat="frame"))), 1)
    assert_eq(len(filter_rules([rule], make_item(product_cat="lens"))),  0)

def t_inactive_ignored():
    rule = make_rule(active=False)
    assert_eq(len(filter_rules([rule], make_item())), 0)

def t_min_qty_condition():
    rule = make_rule(conditions=RuleConditions(min_qty=10))
    assert_eq(len(filter_rules([rule], make_item(quantity=5))),  0)
    assert_eq(len(filter_rules([rule], make_item(quantity=10))), 1)

def t_invoice_multi_line():
    frame_rule = make_rule(id="fr", name="Frame 10%", value=Decimal("10"),
                            conditions=RuleConditions(product_cats=["frame"]))
    lens_rule  = make_rule(id="lr", name="Lens 15%",  value=Decimal("15"),
                            conditions=RuleConditions(product_cats=["lens"]))
    engine = DiscountEngine([frame_rule, lens_rule])
    lines  = [
        LineItem(base_price=Decimal("1200"), quantity=1, product_cat="frame"),
        LineItem(base_price=Decimal("800"),  quantity=2, product_cat="lens"),
    ]
    inv = engine.calculate_invoice(lines)
    assert_eq(inv["totals"]["gross"],          2800.0)
    assert_eq(inv["totals"]["total_discount"],  360.0)

def t_simulate_mode():
    r1 = make_rule(id="r1", name="5%",  value=Decimal("5"))
    r2 = make_rule(id="r2", name="12%", value=Decimal("12"))
    sim = DiscountEngine([r1, r2]).simulate(make_item())
    winners = [e for e in sim["all_evaluated"] if e["is_winner"]]
    assert_eq(len(winners), 1)
    assert_eq(winners[0]["rule_name"], "12%")

def t_result_dict_keys():
    result = DiscountEngine([make_rule()]).calculate(make_item())
    d = result.to_dict()
    for key in ["base_price","quantity","gross_amount","rule_applied",
                "discount_pct","discount_amount","net_amount","gst_rate",
                "gst_amount","final_amount"]:
        assert_true(key in d, f"Missing: {key}")

def t_optical_scenario():
    party_rule = make_rule(id="p", name="Wholesale 12%", type=RuleType.PARTY,
                            value=Decimal("12"), priority=2,
                            conditions=RuleConditions(party_tags=["wholesale"]))
    coat_rule  = make_rule(id="c", name="AR Coating 20%", type=RuleType.COATING,
                            value=Decimal("20"), priority=3,
                            conditions=RuleConditions(product_cats=["ar_coating"]))
    item = LineItem(base_price=Decimal("2500"), quantity=2,
                    product_cat="ar_coating", party_tags=["wholesale"])
    result = DiscountEngine([party_rule, coat_rule]).calculate(item)
    assert_eq(result.rule_applied.name, "Wholesale 12%")

def t_pretty_output():
    rule   = make_rule(value=Decimal("12"))
    item   = make_item(base_price=Decimal("1200"), quantity=1)
    result = DiscountEngine([rule]).calculate(item)
    pretty = result.pretty()
    assert_true("1200" in pretty)
    assert_true("1182.72" in pretty)


# ── Run all tests ─────────────────────────────

ALL_TESTS = [
    ("Percent discount calculation",        t_percent_discount),
    ("GST applied AFTER discount",          t_gst_after_discount),
    ("Fixed ₹ discount",                   t_fixed_discount),
    ("Special price override",              t_special_price),
    ("No discount when no rules",           t_no_discount),
    ("BOGO Buy 10 Get 1",                  t_bogo),
    ("Slab tier selection (3 tiers)",       t_slab),
    ("Special price beats all",             t_special_beats_all),
    ("Higher discount wins same priority",  t_best_discount_wins_same_priority),
    ("Only ONE rule fires (best-wins)",     t_only_one_rule_fires),
    ("Party tag filter match/no-match",     t_party_tag_filter),
    ("Product category filter",             t_product_cat_filter),
    ("Inactive rule ignored",               t_inactive_ignored),
    ("Min qty condition",                   t_min_qty_condition),
    ("Multi-line invoice calculation",      t_invoice_multi_line),
    ("Simulate mode with winner flag",      t_simulate_mode),
    ("Result dict has all required keys",   t_result_dict_keys),
    ("Optical: party beats coating rule",   t_optical_scenario),
    ("Pretty output format",               t_pretty_output),
]

print("\n" + "═"*60)
print("  🔬  OPTICAL DISCOUNT ENGINE — TEST SUITE")
print("═"*60)

for name, fn in ALL_TESTS:
    test(name, fn)

print()
for r in results:
    print(r)

print()
print("═"*60)
status = "✅ ALL PASSED" if failed == 0 else f"⚠️  {failed} FAILED"
print(f"  {status}   ({passed}/{len(ALL_TESTS)} tests passed)")
print("═"*60 + "\n")

sys.exit(0 if failed == 0 else 1)
