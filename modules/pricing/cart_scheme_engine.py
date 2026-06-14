"""
modules/pricing/cart_scheme_engine.py  — revised after audit rejection
=======================================================================
All 5 blockers from audit fixed:
  1. Migration renamed to 0035_cart_scheme_rules.sql
  2. Trailing SELECT removed from migration
  3. Pair-counting fixed: R+L = 1 spectacle pair (not 2 lines)
  4. CL schemes use box/pcs quantity, not line count
  5. GST fields NOT touched — caller's existing pipeline recalculates
  6. FREE physical rewards bill at a nominal value (default ₹1), not ₹0,
     so inventory/accounting audit remains visible.

Pipeline position (caller must maintain):
    raw_price
    → discount_engine
    → supplier_scheme_engine
    → cart_scheme_engine          ← this module
    → existing GST pipeline       ← recalculates gst_amount AFTER this
    → billing_total
"""
from __future__ import annotations
import logging, copy
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_PAIR_EYES = {"R","L","RE","LE","RIGHT","LEFT"}

# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class CartSchemeResult:
    applied: bool = False
    scheme_name: str = ""
    reward_type: str = ""
    buy_qty: float = 0
    reward_qty: float = 0
    repeats: bool = False
    product_category: str = ""
    groups_applied: int = 0
    affected_lines: list = field(default_factory=list)
    dry_run_detail: list = field(default_factory=list)
    message: str = ""
    not_applied_reason: str = ""


# ── DB ────────────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        logger.warning(f"[CartScheme] DB: {e}")
        return []


def _f(v, d=0.0):
    try: return float(v)
    except: return d

def _i(v, d=0):
    try: return int(v)
    except: return d

def _n(v): return str(v or "").strip().lower()


# ── Rule fetch ────────────────────────────────────────────────────────────────

def _fetch_rules(party_id="", order_type=""):
    try:
        rows = _q("""
            SELECT id::text AS rule_id,
                   COALESCE(scheme_name,'')             AS scheme_name,
                   COALESCE(party_filter,'')            AS party_filter,
                   COALESCE(order_type_filter,'')       AS order_type_filter,
                   COALESCE(applies_to_supplier,'')     AS supplier_filter,
                   COALESCE(applies_to_brand,'')        AS brand_filter,
                   COALESCE(applies_to_product,'')      AS product_filter,
                   COALESCE(product_category,'SPECTACLE') AS product_category,
                   COALESCE(same_power_required,FALSE)  AS same_power_required,
                   COALESCE(buy_qty,2)::float            AS buy_qty,
                   COALESCE(reward_qty,1)::float         AS reward_qty,
                   COALESCE(reward_type,'FREE')          AS reward_type,
                   COALESCE(reward_value,0)::float       AS reward_value,
                   COALESCE(free_billing_value,1)::float AS free_billing_value,
                   COALESCE(print_style,'TRANSPARENT')   AS print_style,
                   COALESCE(eligibility_window,'SAME_CART') AS eligibility_window,
                   COALESCE(eligibility_window_days,0)::float AS eligibility_window_days,
                   COALESCE(power_match_mode,'ANY_POWER') AS power_match_mode,
                   COALESCE(tier_pricing_json,'{}'::jsonb) AS tier_pricing_json,
                   COALESCE(repeats,FALSE)               AS repeats
            FROM cart_scheme_rules
            WHERE COALESCE(is_active,TRUE)=TRUE
              AND (valid_from IS NULL OR valid_from<=CURRENT_DATE)
              AND (valid_to   IS NULL OR valid_to  >=CURRENT_DATE)
            ORDER BY buy_qty DESC, reward_qty DESC
        """)
    except Exception:
        return []
    out = []
    for r in rows:
        pf  = _n(r.get("party_filter",""))
        otf = _n(r.get("order_type_filter",""))
        if pf  and party_id   and pf  != _n(party_id):   continue
        if otf and order_type and otf not in _n(order_type): continue
        out.append(r)
    return out


# ── Line classification ───────────────────────────────────────────────────────

def _product_type(line):
    if line.get("is_service_line") or line.get("is_deleted"):
        return "SERVICE"
    lp    = line.get("lens_params") or {}
    eye   = str(line.get("eye_side","")).upper().strip()
    pname = _n(line.get("product_name",""))
    grp   = _n(line.get("main_group","") or lp.get("main_group",""))
    cat   = _n(line.get("category","") or lp.get("category",""))
    if grp in ("frames","frame") or cat in ("frames","frame"):
        return "FRAME"
    is_cl = ("contact" in pname or "6pk" in pname or " pk" in pname
             or "cl" in cat or "contact" in cat)
    if is_cl:
        return "CONTACT_LENS"
    if eye in _PAIR_EYES:
        return "SPECTACLE"
    return "ANY"


def _eligible(line, rule):
    if line.get("is_deleted") or _product_type(line) == "SERVICE":
        return False
    lp  = line.get("lens_params") or {}
    if line.get("manual_price_override") or lp.get("price_locked"):
        return False
    sf  = _n(rule.get("supplier_filter",""))
    bf  = _n(rule.get("brand_filter",""))
    pf  = _n(rule.get("product_filter",""))
    if sf:
        sup = _n(lp.get("supplier_name") or line.get("supplier_name") or "")
        if sf not in sup: return False
    if bf:
        br = _n(line.get("brand") or lp.get("brand") or "")
        if bf not in br: return False
    if pf:
        pn = _n(line.get("product_name") or lp.get("display_product_name") or "")
        if pf not in pn: return False
    return True


def _window_supported(rule) -> bool:
    """Only same-cart/same-punching windows are currently applied in-engine.

    Weekly/monthly/custom windows need order-history accumulation, so they are
    saved for audit and the next phase but deliberately not auto-applied here.
    """
    win = str(rule.get("eligibility_window") or "SAME_CART").upper()
    return win in ("SAME_CART", "SAME_PUNCHING", "SAME_TIME")


# ── Spectacle pair grouping (THE critical fix) ────────────────────────────────

def _spec_key(line, rule):
    """Group key WITHOUT eye_side — R and L of same product share the same key."""
    lp = line.get("lens_params") or {}
    parts = [
        _n(line.get("brand") or lp.get("brand") or ""),
        _n(lp.get("lens_index") or lp.get("index_value") or line.get("lens_index") or ""),
        _n(lp.get("coating") or line.get("coating") or ""),
        _n(lp.get("design") or line.get("design") or ""),
        _n(lp.get("supplier_name") or line.get("supplier_name") or ""),
    ]
    power_mode = str(rule.get("power_match_mode") or "").upper()
    if rule.get("same_power_required") or power_mode in ("SAME_POWER", "SAME_SPH_CYL_AXIS"):
        def _np(v):
            try: return f"{float(v):.2f}"
            except: return ""
        parts += [
            _np(lp.get("sph") or line.get("sph")),
            _np(lp.get("cyl") or line.get("cyl")),
            str(_i(lp.get("axis") or line.get("axis"))),
        ]
    return "|".join(parts)


def _form_pairs(lines, rule):
    """
    Return list of [R_line, L_line] complete pairs.

    WIN54 rule: eye_side R + eye_side L with same product = 1 pair.
    A normal single-spectacle order has 1R + 1L = 1 pair → 1+1 must NOT fire.
    Two spectacle pairs = 2R + 2L = 2 pairs → 1+1 fires.
    """
    buckets: dict[str, dict] = {}
    for l in lines:
        if not _eligible(l, rule): continue
        if _product_type(l) not in ("SPECTACLE","ANY"): continue
        eye = str(l.get("eye_side","")).upper().strip()
        if eye not in _PAIR_EYES: continue
        k = _spec_key(l, rule)
        buckets.setdefault(k, {"R":[], "L":[]})
        side = "R" if eye in ("R","RE","RIGHT") else "L"
        buckets[k][side].append(l)
    pairs = []
    for k, eyes in buckets.items():
        for r, le in zip(eyes["R"], eyes["L"]):
            pairs.append([r, le])
    return pairs


# ── Reward application — does NOT touch gst_amount ───────────────────────────

def _apply_reward(line, rule, reason):
    """
    Adjust unit_price + total_price ONLY.
    gst_amount and gst_percent are intentionally left unchanged.
    The caller's existing GST pipeline recalculates those.
    """
    lp  = line.get("lens_params") or {}
    rt  = str(rule.get("reward_type","FREE")).upper()
    rv  = _f(rule.get("reward_value"), 0.0)
    fv  = max(_f(rule.get("free_billing_value"), 1.0), 0.0)
    op  = _f(line.get("unit_price"), 0.0)
    qty = _f(line.get("billing_qty") or line.get("quantity") or 1, 1.0)

    if rt == "FREE":           np = fv
    elif rt == "PERCENT_OFF":  np = round(op * (1 - rv/100), 2)
    elif rt == "FIXED_PRICE":  np = round(rv, 2)
    else:                      np = op

    net = round(np * qty, 2)
    line["unit_price"]    = np
    line["total_price"]   = net
    line["billing_total"] = net
    # gst_amount deliberately NOT set here

    lp.update({"cart_offer_status":"APPLIED","cart_offer_scheme":rule.get("scheme_name",""),
                "cart_offer_reward_type":rt,"cart_offer_old_price":op,
                "cart_offer_new_price":np,"cart_offer_reason":reason})
    if rt == "FREE":
        lp["cart_offer_nominal_free_value"] = fv
    line["lens_params"] = lp


def _apply_cl_reward(line, rule, free_qty, reason, *, group_qty=0.0):
    """
    CL: reduce billing_total by the value of free_qty boxes.
    Does NOT change billing_qty (physical boxes received stays the same).
    Does NOT touch gst_amount.
    """
    lp  = line.get("lens_params") or {}
    rt  = str(rule.get("reward_type","FREE")).upper()
    rv  = _f(rule.get("reward_value"), 0.0)
    fv  = max(_f(rule.get("free_billing_value"), 1.0), 0.0)
    up  = _f(line.get("unit_price"), 0.0)
    oq  = _f(line.get("billing_qty") or line.get("quantity"), 1.0)
    old_total = round(up * oq, 2)

    if rt == "FREE":
        new_total = round(up * (oq - free_qty) + (free_qty * fv), 2)
    elif rt == "PERCENT_OFF":
        new_total = round(up*(oq-free_qty) + up*free_qty*(1-rv/100), 2)
    else:
        new_total = old_total

    line["billing_total"] = max(0.0, new_total)
    line["total_price"]   = max(0.0, new_total)
    # gst_amount deliberately NOT set here

    lp.update({"cart_offer_status":"APPLIED","cart_offer_scheme":rule.get("scheme_name",""),
                "cart_offer_reward_type":rt,"cart_offer_free_qty":free_qty,
                "cart_offer_old_total":old_total,"cart_offer_new_total":new_total,
                "cart_offer_reason":reason,
                "cart_offer_buy_qty":_f(rule.get("buy_qty"), 0),
                "cart_offer_reward_qty":_f(rule.get("reward_qty"), 0),
                "cart_offer_group_qty":group_qty or (oq + free_qty),
                "cart_offer_paid_qty":max(0.0, (group_qty or oq) - free_qty),
                "cart_offer_print_style":str(rule.get("print_style") or "TRANSPARENT").upper(),
                "cart_offer_average_unit_price":round(new_total / oq, 2) if oq else 0.0})
    if rt == "FREE":
        lp["cart_offer_nominal_free_value"] = fv
    line["lens_params"] = lp


# ── Rule application ──────────────────────────────────────────────────────────

def _apply_rule(lines, rule, dry_run=False):
    cat     = str(rule.get("product_category","SPECTACLE")).upper()
    buy_q   = _f(rule.get("buy_qty"), 2.0)
    rew_q   = _f(rule.get("reward_qty"), 1.0)
    repeats = bool(rule.get("repeats"))
    name    = str(rule.get("scheme_name",""))

    # Require a real rule (guard against empty dict)
    if not name and not rule.get("rule_id"):
        return CartSchemeResult(applied=False, not_applied_reason="Empty rule.")
    if not _window_supported(rule):
        return CartSchemeResult(
            applied=False,
            scheme_name=name,
            not_applied_reason=(
                f"{rule.get('eligibility_window')} is a history-window scheme. "
                "It is saved, but current cart engine applies same-punching/cart schemes only."
            ),
        )

    result = CartSchemeResult(scheme_name=name, reward_type=str(rule.get("reward_type","FREE")).upper(),
                               buy_qty=buy_q, reward_qty=rew_q, repeats=repeats,
                               product_category=cat)

    if cat == "SPECTACLE":
        pairs = _form_pairs(lines, rule)
        ibq = int(buy_q); irq = int(rew_q)
        result.dry_run_detail.append({"category":"SPECTACLE","pairs_found":len(pairs),
                                       "buy_qty":ibq,"reward_qty":irq,
                                       "eligible":len(pairs)>=ibq})
        if len(pairs) < ibq:
            result.not_applied_reason = (
                f"Need {ibq} complete pair(s) (each = 1 R + 1 L line). "
                f"Found {len(pairs)}. A normal single order (1R+1L) = 1 pair and will NOT trigger 1+1.")
            return result

        ga = 0; pos = 0
        while pos + ibq <= len(pairs):
            reward_pairs = pairs[pos + ibq - irq : pos + ibq]
            for pair in reward_pairs:
                for line in pair:
                    reason = f"{name}: pair {ga+1} reward"
                    if not dry_run: _apply_reward(line, rule, reason)
                    result.affected_lines.append(
                        f"{line.get('product_name','')} ({line.get('eye_side','')})")
                    result.dry_run_detail.append({
                        "action":"REWARD","product":line.get("product_name",""),
                        "eye":line.get("eye_side",""),
                        "old_price":_f(line.get("unit_price")),
                        "reward_type":result.reward_type,
                        "reward_value":rule.get("reward_value")})
            ga += 1
            if not repeats: break
            pos += ibq

        if ga:
            result.applied = True; result.groups_applied = ga
            result.message = f"{name} applied {ga}× — {len(result.affected_lines)} line(s) discounted."

    elif cat == "CONTACT_LENS":
        cl_lines = [(l, _f(l.get("billing_qty") or l.get("quantity"), 1.0))
                    for l in lines if _eligible(l, rule) and _product_type(l)=="CONTACT_LENS"]
        if not cl_lines:
            result.not_applied_reason = "No eligible contact lens lines."; return result

        total_qty = sum(qty for _, qty in cl_lines)
        group_qty = buy_q + rew_q
        if group_qty <= 0:
            result.not_applied_reason = "Invalid CL scheme quantity."; return result
        groups = int(total_qty // group_qty) if repeats else (1 if total_qty >= group_qty else 0)
        free_total = groups * rew_q
        result.dry_run_detail.append({
            "category":"CONTACT_LENS","qty_found":total_qty,
            "buy_qty":buy_q,"reward_qty":rew_q,"group_qty":group_qty,
            "free_qty":free_total,"eligible":groups > 0,
            "rule_note":"Buy quantity means paid boxes; reward quantity means extra physical boxes billed nominally."})
        if groups <= 0:
            result.not_applied_reason = (
                f"Need {group_qty:g} eligible CL box(es): {buy_q:g} paid + {rew_q:g} reward. "
                f"Found {total_qty:g}.")
            return result

        ga = groups
        remaining_free = float(free_total)
        for line, qty in sorted(cl_lines, key=lambda x: _f(x[0].get("unit_price"), 0.0)):
            if remaining_free <= 0:
                break
            free_here = min(qty, remaining_free)
            if not dry_run and free_here > 0:
                _apply_cl_reward(
                    line, rule, free_here,
                    f"{name}: {buy_q:g}+{rew_q:g} — {free_total:g} box(es) billed at nominal value",
                    group_qty=group_qty * groups,
                )
                result.affected_lines.append(
                    f"{line.get('product_name','')} ({qty:.0f} boxes, {free_here:.0f} reward)")
            remaining_free -= free_here

        if ga:
            result.applied = True; result.groups_applied = ga
            result.message = f"{name} applied — {buy_q:g}+{rew_q:g}; {free_total:g} CL box(es) billed nominally."
        else:
            result.not_applied_reason = "No CL line had qty >= buy_qty."

    else:  # FRAME / ANY — count eligible lines
        elig = [l for l in lines if _eligible(l, rule) and not l.get("is_deleted")]
        ibq = int(buy_q); irq = int(rew_q)
        result.dry_run_detail.append({"category":cat,"eligible_lines":len(elig),"buy_qty":ibq})
        if len(elig) < ibq:
            result.not_applied_reason = f"Need {ibq} lines, found {len(elig)}."; return result
        ga = 0; pos = 0
        while pos + ibq <= len(elig):
            for line in elig[pos + ibq - irq : pos + ibq]:
                if not dry_run: _apply_reward(line, rule, f"{name}: group {ga+1}")
                result.affected_lines.append(line.get("product_name",""))
            ga += 1
            if not repeats: break
            pos += ibq
        if ga:
            result.applied = True; result.groups_applied = ga
            result.message = f"{name} applied {ga}×."

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def apply_cart_schemes(lines, party_id="", order_type="WHOLESALE", dry_run=False):
    """
    Apply cart-level quantity schemes. Call AFTER supplier_scheme_engine,
    BEFORE GST recalculation. Returns (lines, CartSchemeResult).
    """
    rules = _fetch_rules(party_id=party_id, order_type=order_type)
    if not rules:
        return lines, CartSchemeResult(applied=False, not_applied_reason="No active cart scheme rules.")
    for rule in rules:
        result = _apply_rule(lines, rule, dry_run=dry_run)
        if result.applied:
            return lines, result
    return lines, CartSchemeResult(applied=False, not_applied_reason="No rule matched this cart.")


def preview_cart_schemes(lines, party_id="", order_type="WHOLESALE"):
    """Dry-run: returns analysis dict without modifying lines."""
    lines_copy = copy.deepcopy(lines)
    rules = _fetch_rules(party_id=party_id, order_type=order_type)
    old_total = sum(_f(l.get("billing_total")) for l in lines_copy if not l.get("is_deleted"))
    preview = {"rules_checked":len(rules),"would_apply":False,"old_total":old_total,
               "new_total":None,"rule_that_would_apply":None,"detail":[]}
    for rule in rules:
        r = _apply_rule(lines_copy, rule, dry_run=True)
        preview["detail"].append({"scheme_name":rule.get("scheme_name",""),
            "product_category":rule.get("product_category",""),
            "would_apply":r.applied,"reason":r.not_applied_reason,
            "eligible_lines":r.affected_lines,"dry_run_detail":r.dry_run_detail})
        if r.applied:
            preview["would_apply"] = True
            preview["rule_that_would_apply"] = rule.get("scheme_name","")
            preview["new_total"] = round(
                sum(_f(l.get("billing_total")) for l in lines_copy if not l.get("is_deleted")),2)
            break
    return preview


# ── Admin UI (wire under Pricing & Discount Admin tab) ───────────────────────

def render_cart_scheme_admin(key_prefix="csa"):
    """
    Wire into existing Pricing & Discount Admin panel — NOT standalone.
    In admin_ui.py / pricing admin tab:
        from modules.pricing.cart_scheme_engine import render_cart_scheme_admin
        render_cart_scheme_admin(key_prefix="pa_cart")
    """
    import streamlit as st

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #4c1d95;"
        "border-left:4px solid #8b5cf6;border-radius:8px;padding:10px 16px;margin-bottom:12px'>"
        "<b style='color:#c4b5fd'>\U0001f381 Cart Offers & Quantity Schemes</b>"
        "<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
        "1+1 Free \xb7 Second pair 50% \xb7 12+2 CL \xb7 Buy N get M"
        "</span></div>", unsafe_allow_html=True)

    _rules = _fetch_rules()
    if not _rules:
        st.info("No cart scheme rules yet. Add one below.")
    else:
        for r in _rules:
            rt = r.get("reward_type","FREE"); rv = _f(r.get("reward_value"),0)
            fv = _f(r.get("free_billing_value"), 1)
            rl = (f"Free billed \u20b9{fv:g}" if rt=="FREE" else f"{rv:g}% off" if rt=="PERCENT_OFF" else f"Fixed \u20b9{rv:,.0f}")
            st.markdown(
                f"<div style='background:#0a1628;border-left:3px solid #8b5cf6;"
                f"border-radius:4px;padding:7px 12px;margin-bottom:5px'>"
                f"<b style='color:#c4b5fd'>\U0001f381 {r['scheme_name']}</b>"
                f"<span style='color:#64748b;font-size:0.75rem'> \xb7 {r['product_category']}"
                f" \xb7 Buy {r['buy_qty']} \u2192 {r['reward_qty']} {rl}"
                f"{'  \xb7 Same power' if r.get('same_power_required') else ''}"
                f"{'  \xb7 Repeats' if r.get('repeats') else ''}"
                f"{'  \xb7 Sup: '+r['supplier_filter'] if r.get('supplier_filter') else ''}"
                f"{'  \xb7 Product: '+r['product_filter'] if r.get('product_filter') else ''}"
                f"</span></div>", unsafe_allow_html=True)

    st.markdown("---")
    with st.expander("\u2795 Add Cart Scheme Rule", expanded=False):
        _na1,_na2 = st.columns(2)
        _name = _na1.text_input("Scheme Name", placeholder="e.g. 1+1 Essilor Free", key=f"{key_prefix}_name")
        _cat  = _na2.selectbox("Product Category",["SPECTACLE","CONTACT_LENS","FRAME","ANY"], key=f"{key_prefix}_cat",
                    help="SPECTACLE: R+L pair grouping. CONTACT_LENS: box/pcs qty. FRAME/ANY: line count.")
        _nb1,_nb2 = st.columns(2)
        _unit = "pairs" if _cat=="SPECTACLE" else "boxes" if _cat=="CONTACT_LENS" else "lines"
        _bq = _nb1.number_input(f"Buy qty ({_unit})", min_value=1.0, value=2.0, step=1.0, key=f"{key_prefix}_bq")
        _rq = _nb2.number_input(f"Reward qty ({_unit})", min_value=1.0, value=1.0, step=1.0, key=f"{key_prefix}_rq")
        _nc1,_nc2 = st.columns(2)
        _rt = _nc1.selectbox("Reward Type",["FREE","PERCENT_OFF","FIXED_PRICE"],
                    format_func=lambda x:{"FREE":"Free (bill nominal \u20b91)","PERCENT_OFF":"% Off","FIXED_PRICE":"Fixed Price"}[x],
                    key=f"{key_prefix}_rt")
        if _rt == "FREE":
            _rv = 0.0
            _free_val = _nc2.number_input("Free billing value \u20b9", min_value=0.0, value=1.0, step=1.0, key=f"{key_prefix}_free_val")
        else:
            _rv = _nc2.number_input("Value",min_value=0.0,value=50.0,step=5.0,key=f"{key_prefix}_rv")
            _free_val = 1.0
        _nd1,_nd2,_nd3 = st.columns(3)
        _rep = _nd1.toggle("Repeats",value=False,key=f"{key_prefix}_rep")
        _power_mode = _nd2.selectbox(
            "Power matching",
            ["ANY_POWER", "SAME_POWER", "DIFFERENT_POWER_ALLOWED"],
            format_func=lambda x: {
                "ANY_POWER": "Any / differential powers",
                "SAME_POWER": "Same power only",
                "DIFFERENT_POWER_ALLOWED": "Different powers allowed",
            }.get(x, x),
            key=f"{key_prefix}_power_mode",
            disabled=(_cat not in ("SPECTACLE", "CONTACT_LENS")),
        )
        _sp  = (_power_mode == "SAME_POWER")
        _ot  = _nd3.selectbox("Order type",["","WHOLESALE","RETAIL"],format_func=lambda x:x or "All",key=f"{key_prefix}_ot")
        _nw1, _nw2 = st.columns(2)
        _win = _nw1.selectbox(
            "Grouping window",
            ["SAME_CART", "SAME_PUNCHING_10_MIN", "WEEKLY", "15_DAYS", "MONTHLY", "CUSTOM_DAYS"],
            format_func=lambda x: {
                "SAME_CART": "Same cart / same invoice",
                "SAME_PUNCHING_10_MIN": "Same-time punching (10 min)",
                "WEEKLY": "Weekly",
                "15_DAYS": "15 days",
                "MONTHLY": "Monthly",
                "CUSTOM_DAYS": "Custom days",
            }.get(x, x),
            key=f"{key_prefix}_win",
        )
        _win_days_default = {
            "SAME_CART": 0.0,
            "SAME_PUNCHING_10_MIN": 0.01,
            "WEEKLY": 7.0,
            "15_DAYS": 15.0,
            "MONTHLY": 30.0,
            "CUSTOM_DAYS": 30.0,
        }.get(_win, 0.0)
        _win_days = _nw2.number_input(
            "Window days",
            min_value=0.0,
            value=float(_win_days_default),
            step=1.0,
            disabled=(_win != "CUSTOM_DAYS"),
            key=f"{key_prefix}_win_days",
            help="Same-time 10 min is stored as 0.01 day. History-window enforcement is a next phase.",
        )
        _ne1,_ne2,_ne3,_ne4 = st.columns(4)
        _sf  = _ne1.text_input("Supplier filter",placeholder="blank=all",key=f"{key_prefix}_sf")
        _bf  = _ne2.text_input("Brand filter",   placeholder="blank=all",key=f"{key_prefix}_bf")
        _vf  = _ne3.date_input("Valid from",value=None,key=f"{key_prefix}_vf")
        _vt  = _ne4.date_input("Valid to",  value=None,key=f"{key_prefix}_vt")
        _prod_rows = _q("""
            SELECT id::text AS id, product_name, COALESCE(brand,'') AS brand
            FROM products
            WHERE COALESCE(is_active, TRUE)=TRUE
            ORDER BY product_name
            LIMIT 3000
        """)
        _prod_options = [""] + [r["id"] for r in _prod_rows]
        _prod_name = {r["id"]: str(r.get("product_name") or "") for r in _prod_rows}
        _prod_label = {
            r["id"]: f"{r.get('product_name','')} · {r.get('brand','')}".strip(" ·")
            for r in _prod_rows
        }
        _nf1, _nf2 = st.columns(2)
        _pf_id = _nf1.selectbox(
            "Product",
            _prod_options,
            format_func=lambda x: "All products" if not x else _prod_label.get(x, x),
            key=f"{key_prefix}_pf_id",
            help="Select exact product family. This stores its product name as the scheme filter.",
        )
        _pf = _prod_name.get(_pf_id, "") if _pf_id else ""
        _ps = _nf2.selectbox(
            "Invoice print style",
            ["TRANSPARENT", "AVERAGED"],
            format_func=lambda x: {
                "TRANSPARENT": "Transparent: 12 paid + 2 at ₹1",
                "AVERAGED": "Averaged: all boxes at adjusted box price",
            }.get(x, x),
            key=f"{key_prefix}_print_style",
            help="Accounting stays the same. This only controls customer-facing print display.",
        )
        st.caption(f"Preview: Buy {_bq:g} {_unit} → {_rq:g} {('free billed at \u20b9' + str(_free_val)) if _rt=='FREE' else str(_rv)+('% off' if _rt=='PERCENT_OFF' else ' fixed')}"
                   + (" · repeats" if _rep else "") + (" · same power" if _sp else ""))
        if st.button("\U0001f4be Save Rule",type="primary",use_container_width=True,
                     key=f"{key_prefix}_save",disabled=not _name.strip()):
            import uuid as _u
            try:
                from modules.sql_adapter import run_write as _rw
                _rw("""INSERT INTO cart_scheme_rules
                       (id,scheme_name,is_active,valid_from,valid_to,
                        applies_to_supplier,applies_to_brand,product_category,
                        same_power_required,buy_qty,reward_qty,reward_type,
                        reward_value,free_billing_value,repeats,order_type_filter,
                        applies_to_product,print_style,
                        eligibility_window,eligibility_window_days,power_match_mode,tier_pricing_json)
                   VALUES(%(id)s::uuid,%(n)s,TRUE,%(vf)s,%(vt)s,%(sf)s,%(bf)s,%(cat)s,
                          %(sp)s,%(bq)s,%(rq)s,%(rt)s,%(rv)s,%(fv)s,%(rep)s,%(ot)s,
                          %(pf)s,%(ps)s,%(win)s,%(wdays)s,%(pmode)s,%(tier)s::jsonb)""",
                   {"id":str(_u.uuid4()),"n":_name.strip(),"vf":str(_vf) if _vf else None,
                    "vt":str(_vt) if _vt else None,"sf":_sf.strip() or "","bf":_bf.strip() or "",
                    "cat":_cat,"sp":_sp,"bq":float(_bq),"rq":float(_rq),"rt":_rt,
                    "rv":float(_rv) if _rt!="FREE" else 0.0,
                    "fv":float(_free_val) if _rt=="FREE" else 1.0,
                    "rep":_rep,"ot":_ot or "",
                    "pf":_pf.strip() or "","ps":_ps,
                    "win":_win,
                    "wdays":float(_win_days if _win == "CUSTOM_DAYS" else _win_days_default),
                    "pmode":_power_mode,
                    "tier":"{}"})
                st.success(f"\u2705 '{_name}' saved."); st.rerun()
            except Exception as _e:
                st.error(f"Save failed: {_e}")


# ── Tests ─────────────────────────────────────────────────────────────────────

def _mkt(name,eye,price,brand="Essilor",sup="Essilor",sph=-1.0,cyl=-0.25,ax=180,qty=1,cat=""):
    return {"product_name":name,"eye_side":eye,"brand":brand,
            "unit_price":price,"billing_qty":qty,"quantity":qty,
            "total_price":price*qty,"billing_total":price*qty,
            "gst_percent":5.0,"gst_amount":round(price*qty*0.05,2),
            "discount_amount":0.0,"is_service_line":False,"is_deleted":False,
            "lens_params":{"supplier_name":sup,"brand":brand,"lens_index":"1.56",
                           "coating":"AR","design":"SV","sph":sph,"cyl":cyl,"axis":ax,
                           "category":cat}}


def run_tests():
    """9 tests — 8/8 critical + T9 dealer filter note. No DB required."""
    _1PLUS1 = {"rule_id":"r1","scheme_name":"1+1 Spectacle",
               "party_filter":"","order_type_filter":"","supplier_filter":"","brand_filter":"",
               "product_category":"SPECTACLE","same_power_required":False,
               "buy_qty":2.0,"reward_qty":1.0,"reward_type":"FREE","reward_value":0,"repeats":False}
    _2ND50  = {**_1PLUS1,"rule_id":"r2","scheme_name":"2nd pair 50%",
               "reward_type":"PERCENT_OFF","reward_value":50}
    _SAME   = {**_1PLUS1,"rule_id":"r3","scheme_name":"1+1 Same Power","same_power_required":True}
    _REPT   = {**_1PLUS1,"rule_id":"r4","scheme_name":"1+1 Repeating","repeats":True}
    _SUP    = {**_1PLUS1,"rule_id":"r5","scheme_name":"Bonzer Only","supplier_filter":"bonzer"}
    _CL122  = {"rule_id":"r6","scheme_name":"12+2 CL","party_filter":"","order_type_filter":"",
               "supplier_filter":"","brand_filter":"","product_category":"CONTACT_LENS",
               "same_power_required":False,"buy_qty":12.0,"reward_qty":2.0,
               "reward_type":"FREE","reward_value":0,"repeats":False}

    def _run(desc,rule,lines,expect,extra=None):
        lc = copy.deepcopy(lines)
        r  = _apply_rule(lc,rule,dry_run=False)
        ok = r.applied==expect
        if ok and extra: ok = extra(lc,r)
        print(f"  {chr(0x2705) if ok else chr(0x274c)} {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok: print(f"         Expected applied={expect}, got={r.applied}. {r.not_applied_reason}")
        elif r.applied: print(f"         {r.message}")
        return ok

    print("\n=== Cart Scheme Engine Tests ===\n")
    passed=0; total=0

    # T1 — CRITICAL: Normal R+L = 1 pair must NOT trigger 1+1
    t1=[_mkt("Crizal","R",2000),_mkt("Crizal","L",2000)]
    total+=1; passed+=_run("T1 (CRITICAL): 1R+1L = 1 pair → 1+1 does NOT fire",_1PLUS1,t1,False)

    # T2: Two complete pairs (2R+2L) → 1+1 fires
    t2=[_mkt("Crizal","R",2000),_mkt("Crizal","L",2000),
        _mkt("Crizal","R",2000),_mkt("Crizal","L",2000)]
    total+=1; passed+=_run("T2: 2R+2L = 2 pairs → 1+1 fires",_1PLUS1,t2,True)

    # T3: 2nd pair 50% — reward pair price check
    t3=copy.deepcopy(t2)
    r3=_apply_rule(t3,_2ND50,dry_run=False)
    reward_total=t3[2]["billing_total"]+t3[3]["billing_total"]
    t3ok=r3.applied and abs(reward_total-2000.0)<0.02
    total+=1; passed+=int(t3ok)
    print(f"  {chr(0x2705) if t3ok else chr(0x274c)} {'PASS' if t3ok else 'FAIL'}  "
          f"T3: 2nd pair 50% → reward pair \u20b9{reward_total:.0f} (expected \u20b92000)")

    # T4: Same power, matching
    t4=[_mkt("Crizal","R",2000,sph=-2.0),_mkt("Crizal","L",2000,sph=-2.0),
        _mkt("Crizal","R",2000,sph=-2.0),_mkt("Crizal","L",2000,sph=-2.0)]
    total+=1; passed+=_run("T4: same power required, powers match → fires",_SAME,t4,True)

    # T5: Same power required, but RE and LE powers differ within each pair → no valid pair → no offer
    # Each pair has mismatched RE/LE powers (asymmetric Rx) → no valid same-power pair forms
    t5=[_mkt("Crizal","R",2000,sph=-1.0),_mkt("Crizal","L",2000,sph=-2.0),
        _mkt("Crizal","R",2000,sph=-3.0),_mkt("Crizal","L",2000,sph=-4.0)]
    total+=1; passed+=_run("T5: same power required, RE≠LE power within pairs → does NOT fire",_SAME,t5,False)

    # T6: 4 pairs, once-only → 1 group
    t6=[_mkt("Crizal",e,2000) for e in ["R","L","R","L","R","L","R","L"]]
    r6=_apply_rule(copy.deepcopy(t6),_1PLUS1,dry_run=False)
    t6ok=r6.applied and r6.groups_applied==1
    total+=1; passed+=int(t6ok)
    print(f"  {chr(0x2705) if t6ok else chr(0x274c)} {'PASS' if t6ok else 'FAIL'}  "
          f"T6: 4 pairs once-only → 1 group (got {r6.groups_applied})")

    # T7: 4 pairs, repeating → 2 groups
    r7=_apply_rule(copy.deepcopy(t6),_REPT,dry_run=False)
    t7ok=r7.applied and r7.groups_applied==2
    total+=1; passed+=int(t7ok)
    print(f"  {chr(0x2705) if t7ok else chr(0x274c)} {'PASS' if t7ok else 'FAIL'}  "
          f"T7: 4 pairs repeating → 2 groups (got {r7.groups_applied})")

    # T8: Supplier filter — rule has bonzer filter, line is Essilor → no match
    t8=copy.deepcopy(t2)
    r8=_apply_rule(t8,_SUP,dry_run=False)
    total+=1; passed+=int(not r8.applied)
    print(f"  {chr(0x2705) if not r8.applied else chr(0x274c)} {'PASS' if not r8.applied else 'FAIL'}  "
          f"T8: Supplier filter 'bonzer', lines are Essilor → does NOT fire")

    # T9: CL 12+2 — 14 boxes supplied → 12 paid + 2 nominal-free
    _cl={"product_name":"Air Optix 6PK","eye_side":"R","brand":"Alcon",
         "unit_price":350.0,"billing_qty":14.0,"quantity":14.0,
         "total_price":4900.0,"billing_total":4900.0,"gst_percent":5.0,"gst_amount":245.0,
         "is_service_line":False,"is_deleted":False,
         "lens_params":{"supplier_name":"Alcon","brand":"Alcon","category":"contact lens"}}
    cl=[copy.deepcopy(_cl)]
    r9=_apply_rule(cl,_CL122,dry_run=False)
    t9ok=r9.applied and abs(cl[0]["billing_total"]-4202.0)<0.02
    total+=1; passed+=int(t9ok)
    print(f"  {chr(0x2705) if t9ok else chr(0x274c)} {'PASS' if t9ok else 'FAIL'}  "
          f"T9: CL 12+2 → billing_total=\u20b9{cl[0]['billing_total']:.0f} (expected \u20b94202)")

    print(f"\n=== {passed}/{total} tests passed ===")
    print("NOTE: gst_amount left unchanged — existing WIN54 GST pipeline recalculates after this module.\n")


if __name__ == "__main__":
    run_tests()
