"""
policies/pricing_policy.py — FINAL LOCKED v3.0
================================================

The Policy Layer sits between your billing code and the engine.

WHY THIS EXISTS:
  Engine = frozen math. Never changes.
  Policy = business rules that change. Lives here, not in engine.

  Without this layer:
    Every business change (franchise rules, new channel limits,
    guardrail thresholds) touches the engine → instability.

  With this layer:
    Engine stays frozen. Policy evolves.
    This is the secret to 5+ year stability.

WHAT A POLICY DOES:
  1. Scopes which rules are loaded (by namespace + channel)
  2. Sets margin guardrail thresholds for its context
  3. Sets default conflict strategy
  4. Sets whether margin hard-stops block or just warn
  5. Sets max rules per invoice line (1 = best-wins, >1 = stacking allowed)

USAGE:
  # In your wholesale billing code:
  policy = WHOLESALE_POLICY
  engine = policy.build_engine(all_rules)
  result = engine.calculate(item)

  # In your retail billing:
  policy = RETAIL_POLICY
  engine = policy.build_engine(all_rules)

  # Custom / franchise:
  policy = PricingPolicy(
      name="franchise_nagpur",
      namespace="franchise",
      channel=SalesChannel.ALL,
      soft_warn_pct=Decimal("20"),
      hard_stop_pct=Decimal("10"),
      raise_on_hard_stop=True,
  )
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

try:
    from .discount_rule import DiscountRule, SalesChannel, ConflictStrategy
    from .engine import DiscountEngine
except ImportError:
    from pricing.discount_rule import DiscountRule, SalesChannel, ConflictStrategy
    from pricing.engine import DiscountEngine


# ─────────────────────────────────────────────
# POLICY DEFINITION
# ─────────────────────────────────────────────

@dataclass
class PricingPolicy:
    """
    One pricing policy for one business context.

    Fields:
      name              — human label: "Wholesale Core", "Retail Standard"
      namespace         — scopes rule loading: "core","wholesale","retail","ecommerce","franchise"
      channel           — which channel this policy governs
      soft_warn_pct     — margin % below which soft warning triggers (amber)
      hard_stop_pct     — margin % below which hard stop triggers (red)
      raise_on_hard_stop — True = raise exception on hard stop (franchise-style enforcement)
                           False = surface margin_status="hard_stop" but don't block (default)
      default_conflict_strategy — overrides rule-level conflict_strategy if set
      max_rules_per_line — 1 = best-wins only, >1 = stacking allowed up to N rules
      description       — human notes
    """
    name:                      str
    namespace:                 str             = "core"
    channel:                   SalesChannel    = SalesChannel.ALL
    soft_warn_pct:             Decimal         = Decimal("15")
    hard_stop_pct:             Decimal         = Decimal("5")
    raise_on_hard_stop:        bool            = False
    default_conflict_strategy: Optional[ConflictStrategy] = None
    max_rules_per_line:        int             = 1
    description:               str             = ""

    def build_engine(self, all_rules: List[DiscountRule]) -> DiscountEngine:
        """
        Filter rules to this policy's namespace and channel, then build an engine.

        Rule scoping logic:
          - rules with namespace="core"  are included in ALL policies
          - rules with namespace matching this policy's namespace are included
          - rules with channel=ALL or channel matching this policy's channel are included
        """
        scoped = [
            r for r in all_rules
            if r.active
            and (r.namespace == "core" or r.namespace == self.namespace)
            and (
                r.conditions.channel == SalesChannel.ALL
                or r.conditions.channel == self.channel
                or self.channel == SalesChannel.ALL
            )
        ]

        return DiscountEngine(
            rules              = scoped,
            soft_warn_pct      = self.soft_warn_pct,
            hard_stop_pct      = self.hard_stop_pct,
            raise_on_hard_stop = self.raise_on_hard_stop,
        )

    def describe(self) -> str:
        return (
            f"Policy: {self.name}\n"
            f"  Namespace : {self.namespace}\n"
            f"  Channel   : {self.channel.value}\n"
            f"  Margins   : warn<{self.soft_warn_pct}%  block<{self.hard_stop_pct}%  "
            f"{'(raises)' if self.raise_on_hard_stop else '(warns only)'}\n"
            f"  Max rules : {self.max_rules_per_line} per line\n"
        )


# ─────────────────────────────────────────────
# BUILT-IN POLICIES  (optical domain defaults)
# Override these in your config or DB
# ─────────────────────────────────────────────

WHOLESALE_POLICY = PricingPolicy(
    name              = "Wholesale Core",
    namespace         = "wholesale",
    channel           = SalesChannel.WHOLESALE,
    soft_warn_pct     = Decimal("12"),   # wholesale margins are leaner
    hard_stop_pct     = Decimal("4"),
    raise_on_hard_stop = False,
    description       = "Standard B2B counter billing. Lean margins accepted.",
)

RETAIL_POLICY = PricingPolicy(
    name              = "Retail Standard",
    namespace         = "retail",
    channel           = SalesChannel.RETAIL,
    soft_warn_pct     = Decimal("20"),
    hard_stop_pct     = Decimal("8"),
    raise_on_hard_stop = False,
    description       = "OTC retail walk-in. Offers panel enabled.",
)

ONLINE_POLICY = PricingPolicy(
    name              = "Online / App",
    namespace         = "ecommerce",
    channel           = SalesChannel.ONLINE,
    soft_warn_pct     = Decimal("18"),
    hard_stop_pct     = Decimal("6"),
    raise_on_hard_stop = False,
    description       = "App and website orders. Promo codes enabled.",
)

FRANCHISE_POLICY = PricingPolicy(
    name              = "Franchise",
    namespace         = "franchise",
    channel           = SalesChannel.ALL,
    soft_warn_pct     = Decimal("25"),
    hard_stop_pct     = Decimal("10"),
    raise_on_hard_stop = True,            # Franchise: hard block, not just warning
    description       = "Franchise outlets. Hard margin enforcement. No exceptions.",
)

# Channel → policy map for quick lookup
CHANNEL_POLICY_MAP = {
    SalesChannel.WHOLESALE: WHOLESALE_POLICY,
    SalesChannel.RETAIL:    RETAIL_POLICY,
    SalesChannel.ONLINE:    ONLINE_POLICY,
    SalesChannel.ALL:       WHOLESALE_POLICY,  # default fallback
}


def get_policy(channel: SalesChannel, namespace: str = "core") -> PricingPolicy:
    """
    Return the appropriate policy for a channel + namespace combination.
    Franchise namespace always returns FRANCHISE_POLICY regardless of channel.
    """
    if namespace == "franchise":
        return FRANCHISE_POLICY
    return CHANNEL_POLICY_MAP.get(channel, WHOLESALE_POLICY)
