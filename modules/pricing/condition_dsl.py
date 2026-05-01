"""
core/condition_dsl.py — FINAL LOCKED v3.0
==========================================

Universal Condition DSL Evaluator.

WHY THIS EXISTS:
  Without it: adding a new condition dimension (doctor-specific, region, store-wise)
  requires editing engine.py forever.
  With it: add any new field to LineItem and write a DSL rule — engine never changes.

HOW IT WORKS:
  Rules optionally carry a conditions_dsl dict.
  If set: this evaluator runs instead of legacy hardcoded checks.
  If None: legacy filter_rules() runs (zero migration risk for existing rules).

DSL FORMAT:
  AND block:
    {"op": "all", "conditions": [...]}

  OR block:
    {"op": "any", "conditions": [...]}

  Leaf condition:
    {"field": "...", "op": "...", "value": ...}

  Supported fields:
    party_id, party_tags, product_id, product_cat,
    brand_group, channel, quantity, gross,
    promo_code, namespace, margin_pct

  Supported operators:
    =  !=  in  not_in  any  all  >  >=  <  <=  contains  starts_with

EXAMPLES:
  # Titan wholesale, qty >= 10
  dsl = DSL.all([
      DSL.brand_in(["titan"]),
      DSL.party_tags_any(["wholesale"]),
      DSL.min_qty(10),
  ])

  # Either VIP party OR Titan brand
  dsl = DSL.any([
      DSL.party_tags_any(["vip"]),
      DSL.brand_in(["titan"]),
  ])

  # Nested: (retail OR online) AND product in frame/lens
  dsl = DSL.all([
      DSL.any([DSL.channel_is("retail"), DSL.channel_is("online")]),
      DSL.product_in(["frame","lens"]),
  ])
"""

from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    from .discount_rule import LineItem
    from .engine import _normalize_promo
except ImportError:
    try:
        from pricing.discount_rule import LineItem
        from pricing.engine import _normalize_promo
    except ImportError:
        # Allow standalone import for testing
        def _normalize_promo(code):
            return code.strip().upper() if code else None


# ─────────────────────────────────────────────
# FIELD EXTRACTION
# Maps DSL field names → LineItem values
# Add new fields here as LineItem grows — engine untouched
# ─────────────────────────────────────────────

def _extract(item: LineItem, field: str, margin_pct: Optional[Decimal] = None) -> Any:
    """Return the value of a DSL field from a LineItem."""
    return {
        "party_id":    item.party_id,
        "party_tags":  item.party_tags,
        "product_id":  item.product_id,
        "product_cat": item.product_cat,
        "brand_group": item.brand_group,
        "channel":     item.channel.value if item.channel else "all",
        "namespace":   item.namespace,
        "quantity":    item.quantity,
        "gross":       float(item.gross),
        "promo_code":  _normalize_promo(item.promo_code),
        "margin_pct":  float(margin_pct) if margin_pct is not None else None,
    }.get(field)


# ─────────────────────────────────────────────
# OPERATOR IMPLEMENTATIONS
# ─────────────────────────────────────────────

def _eval_op(item_val: Any, op: str, rule_val: Any) -> bool:
    """Evaluate: item_val <op> rule_val. Returns bool."""

    # None-safety: if item has no value for the field
    if item_val is None:
        if op in ("=", "=="):    return rule_val is None
        if op == "!=":           return rule_val is not None
        if op == "not_in":       return True   # None is never in a list
        return False

    def _up(v):
        return str(v).upper() if isinstance(v, str) else v

    def _up_list(lst):
        return [str(v).upper() for v in lst]

    if op in ("=", "=="):
        return _up(item_val) == _up(rule_val)

    if op == "!=":
        return _up(item_val) != _up(rule_val)

    if op == "in":
        rv = rule_val if isinstance(rule_val, list) else [rule_val]
        if isinstance(item_val, str):
            return item_val.upper() in _up_list(rv)
        return item_val in rv

    if op == "not_in":
        rv = rule_val if isinstance(rule_val, list) else [rule_val]
        if isinstance(item_val, str):
            return item_val.upper() not in _up_list(rv)
        return item_val not in rv

    if op == "any":
        # item_val is a LIST — any element must appear in rule_val list
        iv = item_val if isinstance(item_val, list) else [item_val]
        rv = rule_val if isinstance(rule_val, list) else [rule_val]
        return any(_up(v) in _up_list(rv) for v in iv)

    if op == "all":
        # item_val is a LIST — ALL elements must appear in rule_val list
        iv = item_val if isinstance(item_val, list) else [item_val]
        rv = rule_val if isinstance(rule_val, list) else [rule_val]
        return all(_up(v) in _up_list(rv) for v in iv)

    if op == ">":   return float(item_val) >  float(rule_val)
    if op == ">=":  return float(item_val) >= float(rule_val)
    if op == "<":   return float(item_val) <  float(rule_val)
    if op == "<=":  return float(item_val) <= float(rule_val)

    if op == "contains":
        return str(rule_val).upper() in str(item_val).upper()

    if op == "starts_with":
        return str(item_val).upper().startswith(str(rule_val).upper())

    return False  # Unknown operator = safe miss


# ─────────────────────────────────────────────
# DSL EVALUATOR  (recursive — handles nesting)
# ─────────────────────────────────────────────

def eval_dsl(
    node:       Dict,
    item:       LineItem,
    margin_pct: Optional[Decimal] = None,
) -> bool:
    """
    Recursively evaluate one DSL node against a LineItem.

    Node types:
      Leaf:   {"field": "...", "op": "...", "value": ...}
      Branch: {"op": "all"|"any", "conditions": [...]}
    """
    # Leaf node
    if "field" in node:
        field    = node["field"]
        op       = node.get("op", "=")
        rule_val = node.get("value")
        item_val = _extract(item, field, margin_pct)
        return _eval_op(item_val, op, rule_val)

    # Branch node
    op         = node.get("op", "all")
    conditions = node.get("conditions", [])
    if not conditions:
        return True  # empty = no restriction

    if op == "all":
        return all(eval_dsl(c, item, margin_pct) for c in conditions)
    if op == "any":
        return any(eval_dsl(c, item, margin_pct) for c in conditions)

    return False


def matches_dsl(
    dsl:        Optional[Dict],
    item:       LineItem,
    margin_pct: Optional[Decimal] = None,
) -> bool:
    """
    Public entry point. Safe to call with dsl=None (returns True = no restriction).
    Never raises — engine continues on DSL error.
    """
    if dsl is None:
        return True
    try:
        return eval_dsl(dsl, item, margin_pct)
    except Exception:
        return False  # DSL error = skip this rule, never crash billing


# ─────────────────────────────────────────────
# DSL BUILDER  (fluent API for authoring rules)
# ─────────────────────────────────────────────

class DSL:
    """
    Fluent builder for DSL dicts.
    Use in: rule authoring UI, AI rule generator, tests, API payloads.

    Examples:
        # Titan wholesale, bulk 10+
        dsl = DSL.all([
            DSL.brand_in(["titan"]),
            DSL.party_tags_any(["wholesale"]),
            DSL.min_qty(10),
        ])

        # Online order with promo code
        dsl = DSL.all([
            DSL.channel_is("online"),
            DSL.promo_code("DIWALI25"),
        ])

        # VIP or Gold party, any channel
        dsl = DSL.any([
            DSL.party_tags_any(["vip"]),
            DSL.party_tags_any(["gold"]),
        ])

        # Margin-safe rule: only fires if margin >= 20%
        dsl = DSL.all([
            DSL.party_tags_any(["wholesale"]),
            DSL.min_margin(20),
        ])
    """

    @staticmethod
    def field(name: str, op: str, value: Any) -> dict:
        return {"field": name, "op": op, "value": value}

    @staticmethod
    def all(conditions: List[dict]) -> dict:
        return {"op": "all", "conditions": conditions}

    @staticmethod
    def any(conditions: List[dict]) -> dict:
        return {"op": "any", "conditions": conditions}

    # ── Convenience shortcuts ─────────────────

    @staticmethod
    def party_tags_any(tags: List[str]) -> dict:
        return DSL.field("party_tags", "any", tags)

    @staticmethod
    def product_in(cats: List[str]) -> dict:
        return DSL.field("product_cat", "in", cats)

    @staticmethod
    def brand_in(brands: List[str]) -> dict:
        return DSL.field("brand_group", "in", brands)

    @staticmethod
    def channel_is(channel: str) -> dict:
        return DSL.field("channel", "=", channel)

    @staticmethod
    def namespace_is(ns: str) -> dict:
        return DSL.field("namespace", "=", ns)

    @staticmethod
    def promo_code(code: str) -> dict:
        return DSL.field("promo_code", "=", code.strip().upper())

    @staticmethod
    def min_qty(n: int) -> dict:
        return DSL.field("quantity", ">=", n)

    @staticmethod
    def max_qty(n: int) -> dict:
        return DSL.field("quantity", "<=", n)

    @staticmethod
    def min_gross(amount: float) -> dict:
        return DSL.field("gross", ">=", amount)

    @staticmethod
    def min_margin(pct: float) -> dict:
        """Only apply this rule if margin is currently >= pct%. (margin_safe rules)"""
        return DSL.field("margin_pct", ">=", pct)

    @staticmethod
    def party_is(party_id: str) -> dict:
        return DSL.field("party_id", "=", party_id)
