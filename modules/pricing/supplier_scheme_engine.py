"""
Supplier-party scheme engine.

Used for supplier subscription schemes such as:
    Bonzer + Parakh Opticals, active for 30 days, coating/design upgrades
    at a capped or fixed distribution price.

This module is read-only. It never mutates pricing by itself; callers decide
whether to warn, block, or stamp the computed decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import json
from typing import Any


@dataclass
class SchemeDecision:
    matched: bool
    scheme_id: str = ""
    rule_id: str = ""
    scheme_name: str = ""
    rule_name: str = ""
    message: str = ""
    reward_design: str = ""
    reward_coating: str = ""
    reward_treatment: str = ""
    reward_index: str = ""
    expected_customer_price: float | None = None
    expected_procurement_price: float | None = None
    customer_price_mode: str = ""
    allow_additional_discount: bool = False
    expected_charged_qty: float | None = None
    actual_charged_qty: float | None = None
    bonus_buy_qty: float | None = None
    bonus_free_qty: float | None = None
    scheme_unit: str = ""
    procurement_ok: bool = True
    procurement_delta: float = 0.0
    block_reason: str = ""


def _norm(v: Any) -> str:
    try:
        if v is None:
            return ""
        if isinstance(v, float) and v != v:
            return ""
        s = str(v).strip()
        return "" if s.lower() in ("nan", "none", "nat", "null") else s
    except Exception:
        return ""


def _upper(v: Any) -> str:
    return _norm(v).upper()


def _as_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return None


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_date(v: Any) -> _dt.date:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return _dt.date.fromisoformat(v[:10])
        except ValueError:
            pass
    return _dt.date.today()


def _q(sql: str, params: dict | None = None) -> list[dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _text_match(rule_value: Any, actual: Any, contains: bool = False) -> bool:
    rv = _upper(rule_value)
    if not rv:
        return True
    av = _upper(actual)
    if contains:
        return rv in av
    return av == rv or rv in av


def _product_match(rule: dict, ctx: dict) -> bool:
    pid_rule = _norm(rule.get("match_product_id"))
    if pid_rule and pid_rule != _norm(ctx.get("product_id")):
        return False
    if not _text_match(rule.get("match_brand"), ctx.get("brand")):
        return False
    if not _text_match(rule.get("match_product_name_like"), ctx.get("product_name"), contains=True):
        return False
    if not _text_match(rule.get("match_index"), ctx.get("lens_index") or ctx.get("index")):
        return False
    if not _text_match(rule.get("match_coating"), ctx.get("coating")):
        return False
    if not _text_match(rule.get("match_treatment"), ctx.get("treatment")):
        return False
    if not _text_match(rule.get("match_design"), ctx.get("design")):
        return False
    return True


def _expected_price(mode: str, value: Any, discount_pct: Any, base_price: Any) -> float | None:
    mode = _upper(mode) or "UNCHANGED"
    val = _as_float(value)
    base = _as_float(base_price)
    pct = _as_float(discount_pct)
    if mode in ("", "UNCHANGED"):
        return None
    if mode in ("FIXED", "FIXED_PRICE", "MAX_UNIT_PRICE") and val is not None:
        return round(val, 2)
    if mode == "PERCENT_OFF" and base is not None and pct is not None:
        return round(base * (1 - pct / 100.0), 2)
    return None


def _source_product_price(rule: dict, ctx: dict) -> float | None:
    """
    Source-price upgrade template:
    match the product actually ordered, but price it from a source
    product/coating such as Easy Sure HC for the same index.
    ophthalmic_lens_specs stores pair prices; order lines use per-lens unit.
    """
    meta = _as_dict(rule.get("rule_json"))
    source_product_id = _norm(meta.get("source_product_id"))
    if not source_product_id:
        return None

    idx = _norm(meta.get("source_index")) or _norm(ctx.get("lens_index") or ctx.get("index"))
    coating = _norm(meta.get("source_coating")) or _norm(ctx.get("coating"))
    # source_treatment: blank = use line's treatment (or default Clear)
    _src_treat = _norm(meta.get("source_treatment") or "")
    if _src_treat:
        treatment = _src_treat
    else:
        treatment = _norm(ctx.get("treatment")) or "Clear"
    if not idx or not coating:
        return None

    rows = _q(
        """
        SELECT COALESCE(wlp_per_pair, 0) AS wlp_per_pair,
               COALESCE(srp_per_pair, 0) AS srp_per_pair
        FROM ophthalmic_lens_specs
        WHERE product_id = %(pid)s::uuid
          AND index_value = %(idx)s::numeric
          AND UPPER(COALESCE(coating,'')) = UPPER(%(coat)s)
          AND (
                UPPER(COALESCE(treatment,'Clear')) = UPPER(%(treat)s)
             OR %(treat)s = ''
          )
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY CASE WHEN UPPER(COALESCE(treatment,'Clear')) = UPPER(%(treat)s) THEN 0 ELSE 1 END
        LIMIT 1
        """,
        {"pid": source_product_id, "idx": idx, "coat": coating, "treat": treatment},
    )
    if not rows:
        return None
    row = rows[0]
    order_type = _upper(ctx.get("order_type") or "WHOLESALE")
    pair_price = _as_float(row.get("srp_per_pair" if order_type == "RETAIL" else "wlp_per_pair"))
    if pair_price is None or pair_price <= 0:
        pair_price = _as_float(row.get("wlp_per_pair")) or _as_float(row.get("srp_per_pair"))
    if pair_price is None or pair_price <= 0:
        return None
    return round(pair_price / 2.0, 2)


def _expected_bonus_charged_qty(received_qty: Any, buy_qty: Any, free_qty: Any) -> float | None:
    received = _as_float(received_qty)
    buy = _as_float(buy_qty)
    free = _as_float(free_qty)
    if received is None or buy is None or free is None or received <= 0 or buy <= 0 or free <= 0:
        return None
    pack = buy + free
    full_sets = int(received // pack)
    remainder = received - (full_sets * pack)
    charged = (full_sets * buy) + min(remainder, buy)
    return round(charged, 2)


def active_scheme_rules(
    supplier_id: str = "",
    supplier_name: str = "",
    party_id: str = "",
    on_date: Any = None,
    scope: str = "",
) -> list[dict]:
    """
    Return active rules for supplier + party on the given date.
    scope: filter by scheme_scope (e.g. 'SUPPLIER', 'OWN_PRODUCT',
           'PARTY_SUBSCRIPTION', 'QUANTITY_BONUS', 'COATING_DESIGN_UPGRADE').
    Leave blank to return all scopes.
    """
    d = _as_date(on_date)
    rows = _q(
        """
        SELECT
            s.id::text AS scheme_id,
            s.scheme_name,
            COALESCE(s.scheme_scope, 'SUPPLIER') AS scheme_scope,
            s.supplier_id::text AS supplier_id,
            COALESCE(s.supplier_name,'') AS supplier_name,
            s.party_id::text AS party_id,
            COALESCE(s.party_name,'') AS party_name,
            r.id::text AS rule_id,
            r.*
        FROM supplier_party_schemes s
        JOIN supplier_party_scheme_rules r ON r.scheme_id = s.id
        WHERE COALESCE(s.active, TRUE) = TRUE
          AND COALESCE(r.active, TRUE) = TRUE
          AND %(dt)s::date BETWEEN s.starts_on AND s.ends_on
          AND (%(scope)s = '' OR UPPER(COALESCE(s.scheme_scope,'SUPPLIER')) = UPPER(%(scope)s))
          AND (
                -- Match by UUID if both sides have it
                (%(sid)s <> '' AND s.supplier_id = NULLIF(%(sid)s, '')::uuid)
                -- Match by name when name provided on both sides
             OR (%(sname)s <> '' AND COALESCE(s.supplier_name,'') <> ''
                 AND UPPER(s.supplier_name) = UPPER(%(sname)s))
                -- Scheme has no supplier restriction at all (both NULL/blank)
             OR (s.supplier_id IS NULL AND COALESCE(s.supplier_name,'') = '')
          )
          AND (
                (
                    s.party_id IS NULL
                    AND UPPER(COALESCE(s.assignment_mode, 'ALL_DEALERS')) = 'ALL_DEALERS'
                )
             OR s.party_id = NULLIF(%(pid)s, '')::uuid
             OR EXISTS (
                    SELECT 1
                    FROM supplier_party_scheme_assignments a
                    WHERE a.scheme_id = s.id
                      AND a.party_id = NULLIF(%(pid)s, '')::uuid
                      AND COALESCE(a.active, TRUE) = TRUE
                      AND %(dt)s::date BETWEEN COALESCE(a.starts_on, s.starts_on)
                                           AND COALESCE(a.ends_on, s.ends_on)
                )
          )
        ORDER BY r.priority ASC, s.created_at DESC, r.created_at DESC
        """,
        {
            "dt": d.isoformat(),
            "sid": _norm(supplier_id),
            "sname": _norm(supplier_name),
            "pid": _norm(party_id),
            "scope": _upper(scope),
        },
    )
    return rows


def evaluate_scheme(ctx: dict, scope: str = "") -> SchemeDecision:
    """
    Evaluate one order/procurement line against active supplier-party rules.

    ctx keys commonly used:
      supplier_id, supplier_name, party_id, product_id, product_name, brand,
      lens_index/index, coating, treatment, design, date,
      customer_unit_price, procurement_unit_price, base_customer_price,
      base_procurement_price

    scope: optional filter e.g. 'SUPPLIER', 'OWN_PRODUCT', 'QUANTITY_BONUS'.
    """
    rules = active_scheme_rules(
        supplier_id=_norm(ctx.get("supplier_id")),
        supplier_name=_norm(ctx.get("supplier_name")),
        party_id=_norm(ctx.get("party_id")),
        on_date=ctx.get("date"),
        scope=scope or _norm(ctx.get("scheme_scope")),
    )
    for rule in rules:
        meta = _as_dict(rule.get("rule_json"))
        order_filter = _upper(meta.get("order_type_filter") or meta.get("channel") or "ALL")
        actual_order_type = _upper(ctx.get("order_type") or "WHOLESALE")
        if order_filter in ("RETAIL", "WHOLESALE") and actual_order_type != order_filter:
            continue
        if not _product_match(rule, ctx):
            continue

        cust_mode = _upper(rule.get("customer_price_mode"))
        if cust_mode == "SOURCE_PRODUCT_PRICE":
            cust_expected = _source_product_price(rule, ctx)
        else:
            cust_expected = _expected_price(
                rule.get("customer_price_mode"),
                rule.get("customer_price_value"),
                rule.get("customer_discount_pct"),
                ctx.get("base_customer_price") or ctx.get("customer_unit_price"),
            )
        proc_expected = _expected_price(
            rule.get("procurement_price_mode"),
            rule.get("procurement_price_value"),
            rule.get("procurement_discount_pct"),
            ctx.get("base_procurement_price") or ctx.get("procurement_unit_price"),
        )

        actual_proc = _as_float(ctx.get("procurement_unit_price"))
        expected_charged_qty = None
        actual_charged_qty = None
        procurement_ok = True
        delta = 0.0
        block_reason = ""
        proc_mode = _upper(rule.get("procurement_price_mode"))
        if proc_mode == "BONUS_QTY":
            expected_charged_qty = _expected_bonus_charged_qty(
                ctx.get("received_qty"),
                rule.get("bonus_buy_qty"),
                rule.get("bonus_free_qty"),
            )
            taxable = _as_float(ctx.get("taxable_value"))
            invoice_unit = _as_float(ctx.get("invoice_unit_price")) or actual_proc
            if taxable is not None and invoice_unit and invoice_unit > 0:
                actual_charged_qty = round(taxable / invoice_unit, 2)
            if expected_charged_qty is not None and actual_charged_qty is not None:
                tolerance = max(_as_float(rule.get("tolerance_amount")) or 0.0, 0.02)
                delta = round(actual_charged_qty - expected_charged_qty, 2)
                if actual_charged_qty > expected_charged_qty + tolerance:
                    procurement_ok = False
                    block_reason = (
                        f"Bonus qty scheme mismatch: received {float(ctx.get('received_qty') or 0):.2f} "
                        f"{_norm(rule.get('scheme_unit') or 'unit')}, charged {actual_charged_qty:.2f}; "
                        f"expected charge {expected_charged_qty:.2f} "
                        f"for {_as_float(rule.get('bonus_buy_qty')):.0f}+{_as_float(rule.get('bonus_free_qty')):.0f}."
                    )
        elif proc_expected is not None and actual_proc is not None:
            tolerance = max(
                _as_float(rule.get("tolerance_amount")) or 0.0,
                proc_expected * ((_as_float(rule.get("tolerance_pct")) or 0.0) / 100.0),
            )
            delta = round(actual_proc - proc_expected, 2)
            if actual_proc > proc_expected + tolerance:
                procurement_ok = False
                block_reason = (
                    f"Scheme price exceeded: actual Rs.{actual_proc:.2f}, "
                    f"allowed Rs.{proc_expected:.2f}."
                )

        rewards = [
            x for x in (
                rule.get("reward_design"),
                rule.get("reward_coating"),
                rule.get("reward_treatment"),
                rule.get("reward_index"),
            )
            if _norm(x)
        ]
        msg = f"{rule.get('scheme_name') or 'Scheme'} / {rule.get('rule_name') or 'Rule'}"
        if cust_mode == "SOURCE_PRODUCT_PRICE" and cust_expected is not None:
            meta = _as_dict(rule.get("rule_json"))
            src = _norm(meta.get("source_product_name")) or "source product"
            coat = _norm(meta.get("source_coating"))
            msg += f" -> bill at {src}{(' ' + coat) if coat else ''} price"
        if rewards:
            msg += " -> " + " · ".join(_norm(x) for x in rewards)

        return SchemeDecision(
            matched=True,
            scheme_id=_norm(rule.get("scheme_id")),
            rule_id=_norm(rule.get("rule_id")),
            scheme_name=_norm(rule.get("scheme_name")),
            rule_name=_norm(rule.get("rule_name")),
            message=msg,
            reward_design=_norm(rule.get("reward_design")),
            reward_coating=_norm(rule.get("reward_coating")),
            reward_treatment=_norm(rule.get("reward_treatment")),
            reward_index=_norm(rule.get("reward_index")),
            expected_customer_price=cust_expected,
            customer_price_mode=_upper(rule.get("customer_price_mode") or ""),
            allow_additional_discount=bool(rule.get("allow_additional_discount") or False),
            expected_procurement_price=proc_expected,
            expected_charged_qty=expected_charged_qty,
            actual_charged_qty=actual_charged_qty,
            bonus_buy_qty=_as_float(rule.get("bonus_buy_qty")),
            bonus_free_qty=_as_float(rule.get("bonus_free_qty")),
            scheme_unit=_norm(rule.get("scheme_unit")),
            procurement_ok=procurement_ok,
            procurement_delta=delta,
            block_reason=block_reason,
        )

    return SchemeDecision(matched=False)


def describe_decision(decision: SchemeDecision) -> str:
    if not decision.matched:
        return ""
    bits = [decision.message]
    if decision.expected_customer_price is not None:
        bits.append(f"customer Rs.{decision.expected_customer_price:.2f}")
    if decision.expected_procurement_price is not None:
        bits.append(f"procurement Rs.{decision.expected_procurement_price:.2f}")
    if decision.expected_charged_qty is not None:
        unit = decision.scheme_unit or "unit"
        bits.append(
            f"{decision.bonus_buy_qty:.0f}+{decision.bonus_free_qty:.0f} {unit}: "
            f"charge {decision.expected_charged_qty:.2f}"
        )
    if not decision.procurement_ok and decision.block_reason:
        bits.append(decision.block_reason)
    return " | ".join(bits)


def apply_customer_scheme_to_line(line: dict, party_id: str = "", order_type: str = "WHOLESALE") -> dict:
    """
    Apply the customer-price side of an active supplier scheme to one line.

    This is intentionally conservative:
    - does nothing unless a rule has customer_price_mode producing an expected price
    - stamps lens_params with the scheme decision for audit
    - recalculates gross/net using the existing discount fields
    """
    lp = line.get("lens_params") or {}
    if not isinstance(lp, dict):
        lp = {}
    # Respect manual price overrides — never overwrite a locked line.
    # Backoffice stores this both on the line and inside lens_params.
    if (
        line.get("price_locked")
        or line.get("manual_price_override")
        or lp.get("price_locked")
        or lp.get("manual_price_override")
    ):
        return line
    supplier_name = (
        lp.get("supplier_name")
        or line.get("supplier_name")
        or ("Bonzer Lenses" if "BONZER" in _upper(line.get("brand") or line.get("product_name")) else "")
    )
    ctx = {
        "supplier_id": lp.get("supplier_id") or line.get("supplier_id") or "",
        "supplier_name": supplier_name,
        "party_id": party_id or line.get("party_id") or "",
        "product_id": line.get("product_id") or "",
        "product_name": line.get("product_name") or "",
        "brand": line.get("brand") or "",
        "lens_index": lp.get("lens_index") or lp.get("index_value") or line.get("lens_index") or "",
        "coating": lp.get("coating") or line.get("coating") or "",
        "treatment": lp.get("treatment") or line.get("treatment") or "",
        "design": lp.get("design") or lp.get("corridor") or line.get("design") or line.get("product_name") or "",
        "customer_unit_price": line.get("unit_price"),
        "base_customer_price": line.get("unit_price"),
        "order_type": order_type,
    }
    decision = evaluate_scheme(ctx)
    if not decision.matched or decision.expected_customer_price is None:
        return line

    old_price = _as_float(line.get("unit_price")) or 0.0
    new_price = float(decision.expected_customer_price)
    line["unit_price"] = new_price

    qty = int(line.get("billing_qty") or line.get("quantity") or 1)
    gross = round(new_price * qty, 2)

    # FIXED_PRICE scheme: the scheme price IS the final price.
    # Do not allow existing line discounts to reduce it further —
    # the scheme has already priced in the benefit.
    # PERCENT_OFF or other modes: discount may still apply.
    price_mode = decision.customer_price_mode or ""
    if price_mode in ("FIXED", "FIXED_PRICE", "MAX_UNIT_PRICE", "SOURCE_PRODUCT_PRICE"):
        disc = 0.0  # scheme price is final
        line["discount_amount"] = 0.0
        line["discount_percent"] = 0.0
    else:
        disc = min(max(float(line.get("discount_amount") or 0), 0.0), gross)

    net = round(gross - disc, 2)
    gst_pct = float(line.get("gst_percent") or 0)
    if str(order_type or "").upper() == "RETAIL":
        gst_amt = round(net - (net / (1 + gst_pct / 100)), 2) if gst_pct and net else 0.0
    else:
        gst_amt = round(net * gst_pct / 100, 2) if gst_pct and net else 0.0
    line["total_price"] = net
    line["billing_total"] = net
    line["gst_amount"] = gst_amt
    # Stamp scheme price mode for audit
    lp = line.get("lens_params") or {}
    lp["supplier_scheme_price_mode"] = price_mode
    lp["supplier_scheme_discount_zeroed"] = price_mode in ("FIXED", "FIXED_PRICE", "MAX_UNIT_PRICE", "SOURCE_PRODUCT_PRICE")
    line["lens_params"] = lp

    lp["supplier_scheme_status"] = "APPLIED"
    lp["supplier_scheme_name"] = decision.scheme_name
    lp["supplier_scheme_rule"] = decision.rule_name
    lp["supplier_scheme_old_price"] = old_price
    lp["supplier_scheme_price"] = new_price
    # If allow_additional_discount=True, party/brand discounts can still apply
    # on top of the scheme price (e.g. wholesale volume scheme + party discount).
    # If False (default), scheme price is final — discount engine will skip this line.
    line["supplier_scheme_applied"] = not decision.allow_additional_discount
    line["scheme_applied"]          = not decision.allow_additional_discount
    line["scheme_allows_discount"]  = decision.allow_additional_discount
    # Always stamp the scheme info for audit regardless of stacking
    lp["supplier_scheme_stackable"] = decision.allow_additional_discount
    if decision.reward_design:
        lp["scheme_reward_design"] = decision.reward_design
    if decision.reward_coating:
        lp["scheme_reward_coating"] = decision.reward_coating
    line["lens_params"] = lp
    line["supplier_scheme_rule"] = decision.rule_name
    line["supplier_scheme_price"] = new_price
    return line
