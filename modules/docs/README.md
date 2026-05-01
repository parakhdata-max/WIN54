# Optical Discount Engine — FINAL LOCKED v3.0

**Status: PRODUCTION LOCKED. Do not restructure. Evolve by extending, never rewriting.**

---

## What This Is

A deterministic, policy-controlled, audit-logged discount engine built for optical retail.

Designed for 5–8 year stability. Every architectural decision is intentional.

---

## File Structure

```
optical_discount_engine/
│
├── models/
│   └── discount_rule.py      ← All data models. Edit only to add fields, never remove.
│
├── core/
│   ├── engine.py             ← THE ENGINE. Frozen. Never add business logic here.
│   ├── condition_dsl.py      ← Universal condition evaluator. AI-ready.
│   └── decision_logger.py    ← Audit log writer. Never raises. Never crashes billing.
│
├── policies/
│   └── pricing_policy.py     ← Business rules live HERE, not in engine.
│                               Edit this freely. Engine stays frozen.
│
├── api/
│   └── simulate_api.py       ← POST /pricing/simulate endpoint. Drop into Flask/FastAPI.
│
├── migrations/
│   ├── 001_discount_schema.sql        ← Base schema
│   ├── 002_v2_brand_channel_promo.sql ← Brand groups, channels, promo codes
│   └── 003_final_locked.sql          ← Decision log, margin config, namespaces,
│                                         versioning, conflict strategy, policies
│
├── ui/
│   └── admin_ui.py           ← Streamlit admin. Run: streamlit run ui/admin_ui.py
│
└── tests/
    ├── run_tests_v2.py        ← 36 tests: v2 full coverage
    ├── run_tests_polish.py    ← 34 tests: P1–P5 polish coverage
    └── run_tests_v3_final.py  ← 50 tests: DSL, Policy, Logger, API, Namespace, Version
```

**Total: 120 tests. All passing.**

---

## The Golden Rule

```
Engine  = FROZEN.  Never add business logic.
Policies = EVOLVING. All business rules live in policies/pricing_policy.py.
```

This is the single decision that gives this system 5–8 year stability.

---

## Quick Start (3 Lines)

```python
from core.engine import DiscountEngine
from policies.pricing_policy import WHOLESALE_POLICY
from models.discount_rule import LineItem, SalesChannel

# Load rules once per session (from DB via your adapter)
engine = WHOLESALE_POLICY.build_engine(all_rules)

# Per line calculation
item = LineItem(
    base_price  = unit_price,
    quantity    = quantity,
    product_cat = product_category,
    brand_group = product.brand_group,
    party_tags  = party.tags,
    channel     = SalesChannel.WHOLESALE,
    promo_code  = entered_code,
    cost_price  = product.cost_price,
    namespace   = "wholesale",
)
result = engine.calculate(item)

# Use result
print(result.pretty())
print(result.final_amount)
print(result.margin_status)  # ok | soft_warning | hard_stop
```

---

## Priority Ladder

| Priority | Fires For | Notes |
|---|---|---|
| 1 | Special price | Always wins. No override. |
| 2 | Party contract | Negotiated deals. Beat standard party. |
| 3 | Party / Brand group | Standard discounts. |
| 4 | Product / Coating / Promo code | Product-level and campaign codes. |
| 5 | Offers (slab, BOGO) | Volume and combo deals. |

Within same priority: highest discount amount wins → then highest % → then alphabetical (deterministic).

---

## Policy Layer

Each billing context uses its own policy. Policies scope rule loading and set guardrail thresholds.

```python
from policies.pricing_policy import WHOLESALE_POLICY, RETAIL_POLICY, ONLINE_POLICY, FRANCHISE_POLICY, get_policy

# Automatic routing by channel + namespace
policy = get_policy(SalesChannel.WHOLESALE)
engine = policy.build_engine(all_rules)
```

| Policy | Namespace | Soft Warn | Hard Stop | On Hard Stop |
|---|---|---|---|---|
| Wholesale Core | wholesale | <12% | <4% | warn |
| Retail Standard | retail | <20% | <8% | warn |
| Online / App | ecommerce | <18% | <6% | warn |
| Franchise | franchise | <25% | <10% | **raises exception** |

Custom policy:
```python
policy = PricingPolicy(
    name="My Custom",
    namespace="core",
    channel=SalesChannel.ALL,
    soft_warn_pct=Decimal("20"),
    hard_stop_pct=Decimal("8"),
    raise_on_hard_stop=False,
)
engine = policy.build_engine(all_rules)
```

---

## Margin Guardrails

Three levels, configurable per policy:

- `"ok"` — margin is healthy, proceed
- `"soft_warning"` — show ⚠️ to billing staff, allow sale
- `"hard_stop"` — show 🛑, block or require manager approval

```python
result = engine.calculate(item)
if result.margin_status == "hard_stop":
    # block or require approval
    pass
elif result.margin_status == "soft_warning":
    # show warning to staff
    pass
```

Franchise policy uses `raise_on_hard_stop=True`, which raises `MarginHardStopError` instead of returning status.

---

## Decision Logging

Every billing decision logged for analytics, audit, and future AI training.

```python
from core.decision_logger import DecisionLogger

logger = DecisionLogger(db_conn)
logger.log(
    invoice_id = invoice.id,
    line_id    = line.id,
    item       = item,
    result     = result,
    namespace  = "wholesale",
)

# Log entire invoice at once
logger.log_invoice(invoice_id, items, results)

# Analytics
stats = logger.get_rule_stats(rule_id)
# → {"fire_count": 142, "avg_discount_pct": 11.2, "avg_margin_pct": 18.4, ...}
```

The logger **never raises**. If DB fails, billing continues. Errors stored in `logger.errors`.

---

## Stackable Rules

```python
# Rules with stackable=True accumulate sequentially
rule1 = DiscountRule(..., stackable=True, value=Decimal("10"))  # 10% off
rule2 = DiscountRule(..., stackable=True, value=Decimal("5"))   # 5% off remaining

# gross=1000 → after rule1: 900 → after rule2: 855
# total discount: 145 (14.5%) — compounds, not adds
```

Engine compares stacked total vs best single non-stackable rule and gives customer the better outcome.

---

## Promo Codes

```python
# Rule side
DiscountRule(
    ...
    conditions=RuleConditions(promo_code="DIWALI25", channel=SalesChannel.ONLINE),
    show_in_offers=True,
    icon_emoji="🪔",
)

# Item side — engine normalizes automatically
item = LineItem(..., promo_code="  diwali25  ")  # normalized to "DIWALI25"
```

---

## Offers Panel

```python
# All offers visible to customer (for collapsible panel in retail/online)
offers = engine.list_available_offers(item)

instant = [o for o in offers if not o["requires_code"]]
codes   = [o for o in offers if o["requires_code"]]
```

---

## Simulate API

```python
from api.simulate_api import SimulateRequest, simulate_request

payload = {
    "channel":    "wholesale",
    "namespace":  "core",
    "party_tags": ["wholesale"],
    "items": [
        {"base_price": 1200, "quantity": 10, "product_cat": "frame",
         "brand_group": "titan", "cost_price": 700}
    ]
}

req      = SimulateRequest.from_dict(payload)
response = simulate_request(req, all_rules)
# → {"status": "ok", "policy": "Wholesale Core", "totals": {...}, "lines": [...]}
```

Flask integration:
```python
from api.simulate_api import make_flask_blueprint
bp = make_flask_blueprint(all_rules_loader=get_all_rules)
app.register_blueprint(bp, url_prefix="/api/v1")
# POST /api/v1/pricing/simulate
```

---

## Universal Condition DSL

New rules can be authored with a declarative DSL instead of hardcoded field checks.
Existing rules are unaffected (DSL is optional, stored in `conditions_dsl` column).

```python
from core.condition_dsl import DSL, matches_dsl

# Titan brand, wholesale channel, 10+ qty
dsl = DSL.all([
    DSL.brand_in(["titan"]),
    DSL.party_tags_any(["wholesale"]),
    DSL.min_qty(10),
])

# Store on a rule
rule = DiscountRule(..., conditions_dsl=dsl)

# Evaluate standalone
matches = matches_dsl(rule.conditions_dsl, item)
```

Supported operators: `= != in not_in any all > >= < <= contains starts_with`

---

## Rule Versioning

Never delete a rule. Version it instead.

```python
# Original rule (version=1, parent_rule_id=None)
original = DiscountRule(id="r-001", name="Titan 8%", version=1, ...)

# New version (version=2, parent_rule_id="r-001")
updated = DiscountRule(id="r-002", name="Titan 10%", version=2, parent_rule_id="r-001", ...)

# Then: SET active=FALSE on r-001
# Now r-002 fires. r-001 is in history.
```

---

## Database

Run migrations in order:
```bash
psql -d your_db -f migrations/001_discount_schema.sql
psql -d your_db -f migrations/002_v2_brand_channel_promo.sql
psql -d your_db -f migrations/003_final_locked.sql
```

Analytics views available after migration:
- `v_rule_effectiveness` — fire count, avg discount, avg margin per rule
- `v_dead_rules` — active rules that haven't fired in 30 days
- `v_brand_performance` — discount and margin by brand group
- `v_promo_effectiveness` — promo code usage, ROI, avg margin
- `v_channel_margin_health` — margin health by channel (90-day rolling)

---

## Running Tests

```bash
python tests/run_tests_v2.py          # 36 tests — core engine
python tests/run_tests_polish.py      # 34 tests — P1–P5 polish
python tests/run_tests_v3_final.py    # 50 tests — DSL, Policy, Logger, API
```

All 120 tests must pass before any production deployment.

---

## What NOT to Do

These would break stability:

- ❌ Add business logic to `core/engine.py`
- ❌ Add channel-specific conditions to `filter_rules()`
- ❌ Hardcode any threshold in the engine
- ❌ Remove fields from any model (add only, never remove)
- ❌ Delete old rules (version them, deactivate old)
- ❌ Use microservices, event sourcing, or NoSQL here — not needed

---

## Evolution Path (When You're Ready)

**Now → Phase 3 (Policy-Controlled)**
You are here. Engine is frozen, policies evolve freely.

**Phase 4 — Intelligence Layer (1–2 years)**
Decision log accumulates data → offline analysis scripts →
AI suggests rules → human approves → rule created via `conditions_dsl` →
no engine changes needed.

**Phase 5 — Autonomous Pricing (Far future)**
Self-adjusting discount campaigns with margin guardrails.
Your architecture already supports this. No rewrites needed.

---

## Architecture Summary

```
Billing Code
    │
    ▼
PricingPolicy.build_engine(all_rules)   ← business rules, guardrails, scoping
    │
    ▼
DiscountEngine.calculate(item)          ← frozen math: filter → best-wins → GST → margin
    │
    ▼
DiscountResult                          ← final_amount, rule_name, margin_status
    │
    ▼
DecisionLogger.log(...)                 ← audit trail, analytics, AI training data
```

**Engine lifespan estimate: 5–8 years without redesign.**
