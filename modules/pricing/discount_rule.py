"""
models/discount_rule.py — FINAL LOCKED v3.0
=============================================

All data models for the Optical Discount Engine.
Pure Python dataclasses. No ORM. Plug into any DB adapter.

WHAT'S IN HERE:
  SalesChannel        — wholesale / retail / online / all
  RuleType            — party / product / brand_group / special /
                        offer_bogo / offer_slab / coating / promo_code
  ValueType           — percent / fixed / special_price / bogo
  ConflictStrategy    — best_price / highest_priority / stack / margin_safe
  SlabTier            — single quantity slab
  RuleConditions      — all filter conditions on a rule
  DiscountRule        — one rule, includes namespace + version + conditions_dsl
  LineItem            — input to engine for one billing line
  DiscountResult      — output from engine for one line
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any
import uuid


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class RuleType(str, Enum):
    PARTY       = "party"
    PRODUCT     = "product"
    BRAND_GROUP = "brand_group"
    SPECIAL     = "special"
    OFFER_BOGO  = "offer_bogo"
    OFFER_SLAB  = "offer_slab"
    COATING     = "coating"
    PROMO_CODE  = "promo_code"


class ValueType(str, Enum):
    PERCENT       = "percent"
    FIXED         = "fixed"
    SPECIAL_PRICE = "special_price"
    BOGO          = "bogo"


class SalesChannel(str, Enum):
    WHOLESALE = "wholesale"
    RETAIL    = "retail"
    ONLINE    = "online"
    ALL       = "all"


class ConflictStrategy(str, Enum):
    """
    How to resolve when multiple rules are applicable.

    best_price        — give customer best price (default)
    highest_priority  — strict priority number, ignore discount size
                        (use for contract rules that must always win)
    stack             — accumulate stackable rules sequentially
    margin_safe       — pick highest discount that keeps margin above threshold
    """
    BEST_PRICE        = "best_price"
    HIGHEST_PRIORITY  = "highest_priority"
    STACK             = "stack"
    MARGIN_SAFE       = "margin_safe"


# Priority ladder (lower number = higher priority, fires first)
# 1 = Special price    — hard override, always wins
# 2 = Party contract   — negotiated deals (assign priority=2 to contract rules)
# 3 = Party / Brand    — standard party or brand group
# 4 = Product/Coating/Promo — product-level and promo codes
# 5 = Offers           — slab and BOGO
RULE_PRIORITY_MAP = {
    RuleType.SPECIAL:      1,
    RuleType.PARTY:        3,
    RuleType.BRAND_GROUP:  3,
    RuleType.PRODUCT:      4,
    RuleType.COATING:      4,
    RuleType.PROMO_CODE:   4,
    RuleType.OFFER_SLAB:   5,
    RuleType.OFFER_BOGO:   5,
}
# Note: priority=2 is the "party contract" slot — assign manually on any rule


# ─────────────────────────────────────────────
# SLAB CONFIG
# ─────────────────────────────────────────────

@dataclass
class SlabTier:
    """One quantity tier in a slab-based discount rule."""
    min_qty:      int
    max_qty:      Optional[int]   # None = unlimited
    discount_pct: Decimal

    @classmethod
    def from_dict(cls, d: dict) -> "SlabTier":
        return cls(
            min_qty      = int(d["min_qty"]),
            max_qty      = int(d["max_qty"]) if d.get("max_qty") is not None else None,
            discount_pct = Decimal(str(d["discount_pct"])),
        )

    def matches(self, qty: int) -> bool:
        if qty < self.min_qty:
            return False
        if self.max_qty is not None and qty > self.max_qty:
            return False
        return True


# ─────────────────────────────────────────────
# RULE CONDITIONS
# ─────────────────────────────────────────────

@dataclass
class RuleConditions:
    """
    All filter conditions that gate whether a rule applies to a line item.
    All fields optional — omitted = no restriction on that dimension.

    Fields:
      party_ids       — specific party UUIDs
      party_tags      — e.g. ["wholesale","vip"]
      party_whitelist — rule ONLY applies to these party IDs
      party_blacklist — rule NEVER applies to these party IDs
      product_ids     — specific product UUIDs
      product_cats    — e.g. ["frame","lens","ar_coating"]
      brand_groups    — from products.brand_group: ["titan","rayban"]
      min_qty         — minimum quantity
      max_qty         — maximum quantity
      min_amount      — minimum gross amount
      valid_from      — start date
      valid_to        — end date
      channel         — wholesale / retail / online / all
      promo_code      — exact code match (normalized: uppercase + strip)
    """
    party_ids:       List[str]       = field(default_factory=list)
    party_tags:      List[str]       = field(default_factory=list)
    party_whitelist: List[str]       = field(default_factory=list)
    party_blacklist: List[str]       = field(default_factory=list)
    product_ids:     List[str]       = field(default_factory=list)
    product_cats:    List[str]       = field(default_factory=list)
    brand_groups:    List[str]       = field(default_factory=list)
    min_qty:         Optional[int]   = None
    max_qty:         Optional[int]   = None
    min_amount:      Optional[Decimal] = None
    valid_from:      Optional[date]  = None
    valid_to:        Optional[date]  = None
    channel:         SalesChannel    = SalesChannel.ALL
    promo_code:      Optional[str]   = None

    @classmethod
    def from_dict(cls, d: dict) -> "RuleConditions":
        def _date(val):
            if not val: return None
            return val if isinstance(val, date) else date.fromisoformat(str(val))
        try:
            channel = SalesChannel(d.get("channel", "all"))
        except ValueError:
            channel = SalesChannel.ALL
        return cls(
            party_ids       = d.get("party_ids", []),
            party_tags      = d.get("party_tags", []),
            party_whitelist = d.get("party_whitelist", []),
            party_blacklist = d.get("party_blacklist", []),
            product_ids     = d.get("product_ids", []),
            product_cats    = d.get("product_cats", []),
            brand_groups    = d.get("brand_groups", []),
            min_qty         = d.get("min_qty"),
            max_qty         = d.get("max_qty"),
            min_amount      = Decimal(str(d["min_amount"])) if d.get("min_amount") else None,
            valid_from      = _date(d.get("valid_from")),
            valid_to        = _date(d.get("valid_to")),
            channel         = channel,
            promo_code      = d.get("promo_code"),
        )

    def to_dict(self) -> dict:
        out: dict = {"channel": self.channel.value}
        if self.party_ids:       out["party_ids"]       = self.party_ids
        if self.party_tags:      out["party_tags"]       = self.party_tags
        if self.party_whitelist: out["party_whitelist"]  = self.party_whitelist
        if self.party_blacklist: out["party_blacklist"]  = self.party_blacklist
        if self.product_ids:     out["product_ids"]      = self.product_ids
        if self.product_cats:    out["product_cats"]     = self.product_cats
        if self.brand_groups:    out["brand_groups"]     = self.brand_groups
        if self.min_qty:         out["min_qty"]          = self.min_qty
        if self.max_qty:         out["max_qty"]          = self.max_qty
        if self.min_amount:      out["min_amount"]       = float(self.min_amount)
        if self.valid_from:      out["valid_from"]       = self.valid_from.isoformat()
        if self.valid_to:        out["valid_to"]         = self.valid_to.isoformat()
        if self.promo_code:      out["promo_code"]       = self.promo_code
        return out


# ─────────────────────────────────────────────
# DISCOUNT RULE
# ─────────────────────────────────────────────

@dataclass
class DiscountRule:
    """
    One discount rule.

    Core fields: id, name, type, value_type, value, gst_rate, conditions
    Extended:
      namespace         — scope: core/retail/wholesale/ecommerce/franchise
      conflict_strategy — how conflicts resolve: best_price / highest_priority /
                          stack / margin_safe
      version           — incremented on each edit (keep history via parent_rule_id)
      parent_rule_id    — UUID of previous version (None for original)
      conditions_dsl    — universal condition DSL (dict). If set, evaluated by
                          condition_dsl.py instead of legacy field checks.
                          None = use legacy RuleConditions (zero migration needed)
      stackable         — True = this rule participates in sequential stacking
      show_in_offers    — True = shown in retail/online "Available Offers" panel
      display_label     — short label on invoice line: "Party 12%"
      icon_emoji        — emoji for UI display
    """
    id:            str
    name:          str
    type:          RuleType
    value_type:    ValueType
    gst_rate:      Decimal
    conditions:    RuleConditions

    description:       str = ""
    priority:          int = 4
    value:             Optional[Decimal] = None
    special_price:     Optional[Decimal] = None
    bogo_buy:          Optional[int]     = None
    bogo_get:          Optional[int]     = None
    slab_config:       List[SlabTier]    = field(default_factory=list)
    active:            bool              = True
    created_at:        Optional[datetime] = None

    # ── Future-proofing fields ───────────────────────────────
    namespace:         str               = "core"
    conflict_strategy: ConflictStrategy  = ConflictStrategy.BEST_PRICE
    version:           int               = 1
    parent_rule_id:    Optional[str]     = None   # UUID of previous version
    conditions_dsl:    Optional[dict]    = None   # Universal DSL (see condition_dsl.py)
    stackable:         bool              = False

    # ── UI / display ─────────────────────────────────────────
    display_label:     str  = ""
    icon_emoji:        str  = ""
    show_in_offers:    bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "DiscountRule":
        slabs = []
        if d.get("slab_config"):
            raw = d["slab_config"]
            if isinstance(raw, str):
                import json; raw = json.loads(raw)
            slabs = [SlabTier.from_dict(s) for s in raw]

        cond_raw = d.get("conditions", {})
        if isinstance(cond_raw, str):
            import json; cond_raw = json.loads(cond_raw)

        try:
            cs = ConflictStrategy(d.get("conflict_strategy", "best_price"))
        except ValueError:
            cs = ConflictStrategy.BEST_PRICE

        return cls(
            id                = str(d.get("id", uuid.uuid4())),
            name              = d["name"],
            description       = d.get("description", ""),
            type              = RuleType(d["type"]),
            priority          = int(d.get("priority", 4)),
            value_type        = ValueType(d["value_type"]),
            value             = Decimal(str(d["value"])) if d.get("value") is not None else None,
            special_price     = Decimal(str(d["special_price"])) if d.get("special_price") else None,
            bogo_buy          = d.get("bogo_buy"),
            bogo_get          = d.get("bogo_get"),
            slab_config       = slabs,
            gst_rate          = Decimal(str(d.get("gst_rate", 12))),
            conditions        = RuleConditions.from_dict(cond_raw),
            active            = bool(d.get("active", True)),
            namespace         = d.get("namespace", "core"),
            conflict_strategy = cs,
            version           = int(d.get("version", 1)),
            parent_rule_id    = d.get("parent_rule_id"),
            conditions_dsl    = d.get("conditions_dsl"),
            stackable         = bool(d.get("stackable", False)),
            display_label     = d.get("display_label", ""),
            icon_emoji        = d.get("icon_emoji", ""),
            show_in_offers    = bool(d.get("show_in_offers", False)),
        )


# ─────────────────────────────────────────────
# LINE ITEM
# ─────────────────────────────────────────────

@dataclass
class LineItem:
    """
    Input to the discount engine for one billing line.

    Required:   base_price, quantity
    Optional:   everything else — pass what you have
    """
    base_price:  Decimal
    quantity:    int

    product_id:  Optional[str]    = None
    product_cat: Optional[str]    = None
    party_id:    Optional[str]    = None
    party_tags:  List[str]        = field(default_factory=list)
    gst_rate:    Optional[Decimal] = None
    brand_group: Optional[str]    = None
    channel:     SalesChannel     = SalesChannel.ALL
    promo_code:  Optional[str]    = None
    cost_price:  Optional[Decimal] = None   # for margin simulation

    # Namespace context — engine uses this to scope rule lookup
    namespace:   str              = "core"

    @property
    def gross(self) -> Decimal:
        return self.base_price * self.quantity

    @property
    def cost_gross(self) -> Optional[Decimal]:
        return self.cost_price * self.quantity if self.cost_price else None


# ─────────────────────────────────────────────
# DISCOUNT RESULT
# ─────────────────────────────────────────────

@dataclass
class DiscountResult:
    """Output from engine.calculate() for one line item."""
    base_price:      Decimal
    quantity:        int
    gross_amount:    Decimal
    rule_applied:    Optional[DiscountRule]
    rule_name:       str
    discount_pct:    Decimal
    discount_amount: Decimal
    net_amount:      Decimal
    gst_rate:        Decimal
    gst_amount:      Decimal
    final_amount:    Decimal
    evaluated_rules: List[DiscountRule] = field(default_factory=list)
    cost_gross:      Optional[Decimal]  = None
    margin_amount:   Optional[Decimal]  = None
    margin_pct:      Optional[Decimal]  = None
    margin_status:   str                = "ok"  # ok | soft_warning | hard_stop

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "base_price":      float(self.base_price),
            "quantity":        self.quantity,
            "gross_amount":    float(self.gross_amount),
            "rule_applied":    self.rule_name,
            "discount_pct":    float(self.discount_pct),
            "discount_amount": float(self.discount_amount),
            "net_amount":      float(self.net_amount),
            "gst_rate":        float(self.gst_rate),
            "gst_amount":      float(self.gst_amount),
            "final_amount":    float(self.final_amount),
        }
        if self.margin_amount is not None:
            d["margin_amount"]   = float(self.margin_amount)
            d["margin_pct"]      = float(self.margin_pct or 0)
            d["margin_status"]   = self.margin_status
            d["margin_warning"]  = self.margin_status == "soft_warning"
            d["margin_hard_stop"] = self.margin_status == "hard_stop"
        return d

    def pretty(self) -> str:
        MARGIN_FLAGS = {
            "ok":           " ✅",
            "soft_warning": " ⚠️  LOW MARGIN",
            "hard_stop":    " 🛑 HARD STOP",
        }
        lines = [
            f"  Base Price   : ₹{self.base_price} × {self.quantity} = ₹{self.gross_amount}",
            f"  Rule Applied : {self.rule_name}",
            f"  Discount     : {self.discount_pct:.2f}% = −₹{self.discount_amount:.2f}",
            f"  Net Amount   : ₹{self.net_amount:.2f}",
            f"  GST ({self.gst_rate}%)  : +₹{self.gst_amount:.2f}",
            f"  ─────────────────────────────────────",
            f"  Final Amount : ₹{self.final_amount:.2f}",
        ]
        if self.margin_amount is not None:
            flag = MARGIN_FLAGS.get(self.margin_status, "")
            lines.append(
                f"  Margin       : ₹{self.margin_amount:.2f} "
                f"({self.margin_pct:.1f}%){flag}"
            )
        return "\n".join(lines)
