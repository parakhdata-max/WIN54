from modules.pricing.db import fetch_all
from modules.pricing.engine import DiscountEngine
from modules.pricing.discount_rule import DiscountRule
import json


def load_active_policy(channel="retail", namespace="default"):
    policy = fetch_all("""
        SELECT * FROM pricing.policies
        WHERE active = TRUE
        AND channel = %s
        AND namespace = %s
        LIMIT 1
    """, (channel, namespace))
    return policy[0] if policy else None


# DB may store rule_type in uppercase or with different naming than the engine enum.
# Valid RuleType values: party, product, brand_group, special,
#                        offer_bogo, offer_slab, coating, promo_code
_RULE_TYPE_MAP = {
    # Uppercase variants
    "PARTY":       "party",
    "PRODUCT":     "product",
    "BRAND_GROUP": "brand_group",
    "SPECIAL":     "special",
    "OFFER_BOGO":  "offer_bogo",
    "OFFER_SLAB":  "offer_slab",
    "COATING":     "coating",
    "PROMO_CODE":  "promo_code",
    # DB may use value-type names for rule_type — map to closest intent
    "PERCENT":     "party",
    "FLAT":        "party",
    "FIXED":       "party",
    "DISCOUNT":    "party",
}

def _resolve_rule_type(raw: str) -> str:
    """Normalize DB rule_type value to a valid RuleType enum string."""
    if not raw:
        return "party"
    lowered = raw.strip().lower()
    # Already a valid enum value
    valid = {"party","product","brand_group","special",
             "offer_bogo","offer_slab","coating","promo_code"}
    if lowered in valid:
        return lowered
    # Try uppercase map
    return _RULE_TYPE_MAP.get(raw.strip().upper(), "party")


def _normalize_row(d: dict) -> dict:
    """
    Bridge DB column names -> DiscountRule.from_dict() expected keys.

    DB schema uses:  rule_type, starts_at, ends_at
    from_dict needs: type,      valid_from, valid_to  (inside conditions)

    DB schema is also missing: value_type, gst_rate
    These get safe defaults so from_dict() does not KeyError.
    """
    # Parse conditions JSON if stored as string
    conditions = d.get("conditions") or {}
    if isinstance(conditions, str):
        conditions = json.loads(conditions)

    # Move date range columns into conditions where from_dict() reads them
    if d.get("starts_at"):
        conditions.setdefault("valid_from", d["starts_at"])
    if d.get("ends_at"):
        conditions.setdefault("valid_to", d["ends_at"])

    return {
        **d,
        "type":       _resolve_rule_type(d.get("rule_type", "")),
        "value_type": d.get("value_type", "percent"),  # missing in DB -> safe default
        "gst_rate":   d.get("gst_rate", 12),           # missing in DB -> safe default
        "conditions": conditions,
    }


def load_rules_for_policy(policy_id):
    rows = fetch_all("""
        SELECT r.*
        FROM pricing.discount_rules r
        JOIN pricing.policy_rules pr ON pr.rule_id = r.id
        WHERE pr.policy_id = %s
        AND r.active = TRUE
    """, (policy_id,))

    if not rows:    # fetch_all returns None or [] when no rows -- guard both
        return []

    rules = []
    for r in rows:
        d = _normalize_row(dict(r))
        rules.append(DiscountRule.from_dict(d))

    return rules


def build_live_engine(channel="retail", namespace="default"):
    policy = load_active_policy(channel, namespace)
    if not policy:
        raise Exception("No active pricing policy found for "
                        f"channel={channel!r}, namespace={namespace!r}")

    rules = load_rules_for_policy(policy["id"])
    engine = DiscountEngine(rules=rules)
    return engine, policy
