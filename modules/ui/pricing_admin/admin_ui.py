"""
modules/ui/pricing_admin/admin_ui.py — v4.0 (WIN51)
=====================================================
Discount Rule Admin — embedded in DV ERP via app.py.

Registered in app.py as:
    safe_import("Pricing Admin", "modules.ui.pricing_admin.admin_ui", "render_pricing_admin")

Tabs:
  1. 📋 Active Rules     — grouped by channel + type, with filter
  2. 🧮 Simulator        — test a line item against all rules, see margin
  3. 🧾 Invoice Builder  — multi-line invoice preview
  4. 🏷️ Offers Panel     — what retail/online customers see
  5. ➕ Add Rule         — create a new discount rule in DB
  6. ✅ Validator Test   — test order validation inline

Sidebar:
  - How to use guide
  - Rule type & priority reference
  - File map (which file does what)
"""

import sys, os
_HERE    = os.path.dirname(os.path.abspath(__file__))
_MODULES = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

import streamlit as st
from decimal import Decimal
import json

from pricing.discount_rule import (
    DiscountRule, LineItem, RuleType, ValueType,
    RuleConditions, SlabTier, SalesChannel,
)
from pricing.engine import DiscountEngine

# ── CSS ─────────────────────────────────────────────────────────────────────
_CSS = """<style>
.channel-badge-wholesale{background:#0ea5e9;color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.channel-badge-retail   {background:#10b981;color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.channel-badge-online   {background:#8b5cf6;color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.channel-badge-all      {background:#6b7280;color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.promo-badge{background:#f59e0b;color:#1a1a1a;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:800}
.margin-ok  {color:#10b981;font-weight:700}
.margin-warn{color:#f59e0b;font-weight:700}
.margin-stop{color:#ef4444;font-weight:700}
.winner-row {background:#1e2d1e;border-left:3px solid #10b981;padding:6px 10px;border-radius:4px;margin:4px 0}
.sev-error  {background:#fef2f2;border-left:4px solid #ef4444;padding:8px 12px;border-radius:4px;margin:4px 0}
.sev-warning{background:#fffbeb;border-left:4px solid #f59e0b;padding:8px 12px;border-radius:4px;margin:4px 0}
.sev-ok     {background:#f0fdf4;border-left:4px solid #10b981;padding:8px 12px;border-radius:4px;margin:4px 0}
</style>"""

CHANNEL_BADGE = {
    "wholesale": '<span class="channel-badge-wholesale">WHOLESALE</span>',
    "retail":    '<span class="channel-badge-retail">RETAIL</span>',
    "online":    '<span class="channel-badge-online">ONLINE</span>',
    "all":       '<span class="channel-badge-all">ALL</span>',
}
TYPE_ICON = {
    "party":"👤","product":"📦","brand_group":"🏷️","special":"⚡",
    "offer_bogo":"🎁","offer_slab":"📊","coating":"✨","promo_code":"🎟️",
}

def _badge(ch): return CHANNEL_BADGE.get(ch, ch)
def _icon(t):   return TYPE_ICON.get(t, "•")


# ── Sample rules (used when no DB) ──────────────────────────────────────────
def _sample_rules():
    return [
        DiscountRule(
            id="r1", name="Wholesale Standard 12%", description="All wholesale parties",
            type=RuleType.PARTY, value_type=ValueType.PERCENT, value=Decimal("12"),
            gst_rate=Decimal("12"), priority=3, active=True,
            conditions=RuleConditions(party_tags=["wholesale"], channel=SalesChannel.WHOLESALE),
            display_label="Party 12%", icon_emoji="👤", show_in_offers=False,
        ),
        DiscountRule(
            id="r2", name="VIP Gold 18%", description="VIP and gold tier parties",
            type=RuleType.PARTY, value_type=ValueType.PERCENT, value=Decimal("18"),
            gst_rate=Decimal("12"), priority=2, active=True,
            conditions=RuleConditions(party_tags=["vip","gold"], channel=SalesChannel.WHOLESALE),
            display_label="VIP 18%", icon_emoji="⭐", show_in_offers=False,
        ),
        DiscountRule(
            id="r3", name="Titan Brand Group 8%", description="Titan brand products, wholesale",
            type=RuleType.BRAND_GROUP, value_type=ValueType.PERCENT, value=Decimal("8"),
            gst_rate=Decimal("12"), priority=3, active=True,
            conditions=RuleConditions(brand_groups=["titan"], party_tags=["wholesale"], channel=SalesChannel.WHOLESALE),
            display_label="Titan 8%", icon_emoji="🏷️", show_in_offers=False,
        ),
        DiscountRule(
            id="r4", name="Bulk Frames Slab", description="5% on 10+, 10% on 25+, 15% on 50+",
            type=RuleType.OFFER_SLAB, value_type=ValueType.PERCENT, value=Decimal("0"),
            gst_rate=Decimal("12"), priority=5, active=True,
            conditions=RuleConditions(product_cats=["frame"], channel=SalesChannel.ALL),
            slab_config=[
                SlabTier(min_qty=10, max_qty=24, discount_pct=Decimal("5")),
                SlabTier(min_qty=25, max_qty=49, discount_pct=Decimal("10")),
                SlabTier(min_qty=50, max_qty=None, discount_pct=Decimal("15")),
            ],
            display_label="Bulk Frames", icon_emoji="📊", show_in_offers=True,
        ),
        DiscountRule(
            id="r5", name="CL Buy 10 Get 1 Free", description="BOGO on contact lenses",
            type=RuleType.OFFER_BOGO, value_type=ValueType.BOGO,
            gst_rate=Decimal("18"), priority=5, active=True,
            bogo_buy=10, bogo_get=1,
            conditions=RuleConditions(product_cats=["contact_lens"], channel=SalesChannel.ALL),
            display_label="CL BOGO", icon_emoji="🎁", show_in_offers=True,
        ),
        DiscountRule(
            id="r6", name="App Download — NEWAPP20", description="20% off, code NEWAPP20",
            type=RuleType.PROMO_CODE, value_type=ValueType.PERCENT, value=Decimal("20"),
            gst_rate=Decimal("12"), priority=3, active=True,
            conditions=RuleConditions(channel=SalesChannel.ONLINE, promo_code="NEWAPP20"),
            display_label="App Offer 20%", icon_emoji="📱", show_in_offers=True,
        ),
        DiscountRule(
            id="r7", name="AR Coating 20%", description="Anti-reflective coating upgrade",
            type=RuleType.COATING, value_type=ValueType.PERCENT, value=Decimal("20"),
            gst_rate=Decimal("18"), priority=4, active=True,
            conditions=RuleConditions(product_cats=["ar_coating"], channel=SalesChannel.ALL),
            display_label="AR 20%", icon_emoji="✨", show_in_offers=True,
        ),
    ]


def _load_rules_from_db():
    """Load rules from discount_rules table via sql_adapter."""
    try:
        from modules.sql_adapter import run_query
        import json as _json
        rows = run_query("""
            SELECT id::text, name, description,
                   type,
                   priority, value_type, value, special_price,
                   bogo_buy, bogo_get,
                   slab_config::text AS slab_config,
                   gst_rate,
                   conditions::text AS conditions,
                   active,
                   COALESCE(
                       CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns
                            WHERE table_name='discount_rules' AND column_name='conflict_strategy')
                       THEN conflict_strategy ELSE NULL END, 'best_price'
                   ) AS conflict_strategy,
                   'core' AS namespace,
                   FALSE AS stackable,
                   '' AS display_label,
                   '' AS icon_emoji,
                   FALSE AS show_in_offers
            FROM discount_rules
            WHERE active = TRUE
            ORDER BY priority ASC, name ASC
        """, {}) or []
        rules = []
        for row in rows:
            if row.get("slab_config") and isinstance(row["slab_config"], str):
                row["slab_config"] = _json.loads(row["slab_config"])
            if row.get("conditions") and isinstance(row["conditions"], str):
                row["conditions"] = _json.loads(row["conditions"])
            try:
                rules.append(DiscountRule.from_dict(row))
            except Exception:
                pass
        return rules if rules else _sample_rules()
    except Exception:
        return _sample_rules()


def _save_rule_to_db(rule_data: dict) -> bool:
    """Save a new discount rule to DB."""
    try:
        import json as _json
        from modules.sql_adapter import run_write
        run_write("""
            INSERT INTO discount_rules (
                name, description, type, value_type, value, special_price,
                bogo_buy, bogo_get, slab_config, gst_rate, conditions,
                priority, active,
                stackable, conflict_strategy
            ) VALUES (
                %(name)s, %(desc)s, %(type)s, %(vtype)s, %(val)s, %(sp)s,
                %(bb)s, %(bg)s, %(slab)s::jsonb, %(gst)s, %(cond)s::jsonb,
                %(pri)s, TRUE,
                %(stack)s, %(cs)s
            )
        """, {
            "name":  rule_data["name"],
            "desc":  rule_data.get("description",""),
            "type":  rule_data["type"],
            "vtype": rule_data["value_type"],
            "val":   rule_data.get("value"),
            "sp":    rule_data.get("special_price"),
            "bb":    rule_data.get("bogo_buy"),
            "bg":    rule_data.get("bogo_get"),
            "slab":  _json.dumps(rule_data.get("slab_config") or []),
            "gst":   rule_data.get("gst_rate", 12),
            "cond":  _json.dumps(rule_data.get("conditions", {})),
            "pri":   rule_data.get("priority", 4),
            "cs":    rule_data.get("conflict_strategy","best_price"),
            "ns":    rule_data.get("namespace","core"),
            "stack": rule_data.get("stackable", False),
            "dlbl":  rule_data.get("display_label",""),
            "icon":  rule_data.get("icon_emoji",""),
            "sio":   rule_data.get("show_in_offers", False),
        })
        return True
    except Exception as e:
        import streamlit as st
        st.error(f"Save failed: {e}")
        return False


def _deactivate_rule(rule_id: str) -> bool:
    """Deactivate a rule by ID."""
    try:
        from modules.sql_adapter import run_write
        run_write("UPDATE discount_rules SET active=FALSE, updated_at=NOW() WHERE id=%(id)s::uuid",
                  {"id": rule_id})
        return True
    except Exception:
        return False


# ── SIDEBAR ──────────────────────────────────────────────────────────────────

# ── PHASE 3F: ALERT ENGINE ────────────────────────────────────────────────────
import time as _time

_ALERT_CACHE: dict = {}
_ALERT_CACHE_TS: float = 0.0
_ALERT_CACHE_TTL: float = 300.0  # 5 minutes


def _fetch_alerts() -> list:
    """
    Phase 3F — Real-time alert engine.
    Queries the last 24h of order_lines for:
      1. 🛑 Margin hard-stops — products sold below 5% margin
      2. ⚡ Discount spike    — any rule averaging >30% today
      3. ♻️  Rule overuse      — a single rule fired >50x today

    Returns list of {level, message} dicts.
    Cached 5 minutes. Never blocks sidebar on failure.
    """
    global _ALERT_CACHE, _ALERT_CACHE_TS
    now = _time.time()
    if _ALERT_CACHE and (now - _ALERT_CACHE_TS) < _ALERT_CACHE_TTL:
        return _ALERT_CACHE.get("alerts", [])

    alerts = []
    try:
        from modules.sql_adapter import run_query

        # ── Alert 1: Margin hard-stops in last 24h ────────────────────────
        hard_stops = run_query("""
            SELECT
                COALESCE(p.product_name, 'Unknown') AS product,
                COUNT(*) AS count
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.margin_status = 'hard_stop'
              AND o.created_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            GROUP BY p.product_name
            ORDER BY count DESC
            LIMIT 3
        """) or []

        for r in hard_stops:
            alerts.append({
                "level": "error",
                "message": (
                    f"🛑 **Margin hard-stop** — {r['product']} "
                    f"sold {r['count']}× below 5% margin today"
                ),
            })

        # ── Alert 2: Discount spike — any rule avg >30% today ─────────────
        spikes = run_query("""
            SELECT
                COALESCE(ol.discount_rule, 'Unknown') AS rule_name,
                ROUND(AVG(ol.discount_percent), 1)    AS avg_pct,
                COUNT(*)                               AS lines
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(ol.discount_percent, 0) > 30
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            GROUP BY ol.discount_rule
            HAVING COUNT(*) >= 2
            ORDER BY avg_pct DESC
            LIMIT 2
        """) or []

        for r in spikes:
            alerts.append({
                "level": "warning",
                "message": (
                    f"⚡ **Discount spike** — rule '{r['rule_name']}' "
                    f"averaged {r['avg_pct']}% on {r['lines']} lines today"
                ),
            })

        # ── Alert 3: Rule overuse — any rule fired >50x today ─────────────
        overuse = run_query("""
            SELECT
                COALESCE(ol.discount_rule, 'Unknown') AS rule_name,
                COUNT(*) AS fires
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - INTERVAL '24 hours'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND ol.discount_rule IS NOT NULL
              AND ol.discount_rule != ''
            GROUP BY ol.discount_rule
            HAVING COUNT(*) > 50
            ORDER BY fires DESC
            LIMIT 2
        """) or []

        for r in overuse:
            alerts.append({
                "level": "warning",
                "message": (
                    f"♻️ **Rule overuse** — '{r['rule_name']}' "
                    f"fired {r['fires']}× today — verify intent"
                ),
            })

    except Exception:
        pass  # never block sidebar

    _ALERT_CACHE["alerts"] = alerts
    _ALERT_CACHE_TS = now
    return alerts


def _render_alert_badge():
    """
    Phase 3F — Render alert badges in sidebar.
    Called at top of _render_sidebar on every page load.
    """
    alerts = _fetch_alerts()
    if not alerts:
        return

    errors   = [a for a in alerts if a["level"] == "error"]
    warnings = [a for a in alerts if a["level"] == "warning"]

    if errors:
        import streamlit as st
        st.sidebar.error(
            f"🚨 **{len(errors)} Margin Alert{'s' if len(errors) > 1 else ''}**  \n"
            + "  \n".join(a["message"] for a in errors)
        )
    if warnings:
        import streamlit as st
        st.sidebar.warning(
            f"⚠️ **{len(warnings)} Pricing Warning{'s' if len(warnings) > 1 else ''}**  \n"
            + "  \n".join(a["message"] for a in warnings)
        )


def _render_sidebar():
    with st.sidebar:
        st.markdown("---")
        st.markdown("## 💲 Pricing Admin")

        # ── Phase 3F: Alert Engine ────────────────────────────────────────────
        # Real-time warnings: margin hard-stops, discount spikes, rule abuse
        # Runs on every page load. Cached 5 min to avoid hammering DB.
        _render_alert_badge()

        with st.expander("📖 How to Use", expanded=False):
            st.markdown("""
**1. View Rules** → Active Rules tab  
See all discount rules grouped by channel.  
Filter by channel or rule type.

**2. Test a Rule** → Simulator tab  
Enter: product price, qty, party tags, channel.  
See which rule wins and why, plus margin analysis.

**3. Preview Invoice** → Invoice Builder  
Add multiple lines, see totals with GST.

**4. Customer Offers** → Offers Panel  
Shows what retail/online customers see.

**5. Create Rule** → Add Rule tab  
Fill the form. Rule is saved to `discount_rules` table.

**6. Test Validation** → Validator Test  
Paste or build an order dict, run all 6 validators.
""")

        with st.expander("⚡ Priority Ladder", expanded=False):
            st.markdown("""
| Priority | Type | Wins over |
|---|---|---|
| 1 | Special Price | Everything |
| 2 | Party Contract | 3,4,5 |
| 3 | Party / Brand Group | 4,5 |
| 4 | Product / Coating / Promo | 5 |
| 5 | Offers (Slab, BOGO) | — |

**Within same priority:** highest discount amount wins.  
**Stackable rules** accumulate; result shown if > best single.
""")

        with st.expander("📁 File Map", expanded=False):
            st.markdown("""
```
modules/pricing/
 engine.py          ← DiscountEngine (main)
 discount_rule.py   ← Models (Rule, LineItem, Result)
 discount_adapter.py← DB CRUD
 pricing_policy.py  ← Policy presets
 billing_engine.py  ← compute_line_totals()
 tax_engine.py      ← apply_taxes()
 price_resolver.py  ← resolve price by order type
 decision_logger.py ← audit trail
 condition_dsl.py   ← DSL builder
 live_adapter.py    ← build_live_engine()
 shadow_mode.py     ← A/B shadow testing

modules/validators/
 engine.py          ← ValidationEngine.run()
 registry.py        ← rule name → class map
 order_validator.py ← structure, qty, batches
 party_validator.py ← blocklist check
 product_validator.py← discontinued check
 rx_validator.py    ← SPH/CYL/AXIS/ADD range
 financial_validator← credit, price, total
 tax_validator.py   ← GST slab check

config/
 validation_rules.json ← enable/disable validators
 validation_config.py  ← blocked parties, RX limits

modules/
 validation_gateway.py ← validate_before_submit()
 core/validators_builtin.py ← line-level validators
 core/validation_result.py  ← ValidationIssue class
```
""")

        with st.expander("🔧 Rule Types", expanded=False):
            for rt, icon in TYPE_ICON.items():
                st.markdown(f"{icon} **{rt}**")

        with st.expander("💡 GST Slabs", expanded=False):
            st.markdown("""
| Slab | Products |
|---|---|
| 0% | Basic food, books |
| 5% | Medical devices |
| 12% | Optical frames, lenses |
| 18% | Contact lenses, AR coating |
| 28% | Luxury items |

*GST is applied on post-discount net amount.*
""")


# ── TAB 1: ACTIVE RULES ──────────────────────────────────────────────────────
def _tab_active_rules(rules):
    st.markdown("### 📋 Active Discount Rules")

    c1, c2, c3 = st.columns(3)
    ch_filter   = c1.selectbox("Channel", ["All","wholesale","retail","online"])
    type_filter = c2.selectbox("Rule Type", ["All"] + [t.value for t in RuleType])
    c3.metric("Total Active", len(rules))

    filtered = rules
    if ch_filter != "All":
        filtered = [r for r in filtered if r.conditions.channel.value == ch_filter]
    if type_filter != "All":
        filtered = [r for r in filtered if r.type.value == type_filter]

    if not filtered:
        st.info("No rules match the current filter.")
        return

    groups = {}
    for r in filtered:
        key = (r.conditions.channel.value, r.type.value)
        groups.setdefault(key, []).append(r)

    for (ch, rtype), grp in sorted(groups.items()):
        icon = _icon(rtype)
        st.markdown(
            f"#### {icon} {rtype.upper()} — {_badge(ch)}",
            unsafe_allow_html=True
        )
        for rule in sorted(grp, key=lambda r: r.priority):
            with st.container(border=True):
                ca, cb, cc, cd = st.columns([3, 1, 1, 1])
                ca.markdown(
                    f"**{rule.icon_emoji or icon} {rule.name}**  \n"
                    f"<span style='color:#94a3b8;font-size:0.75rem'>{rule.description or ''}</span>",
                    unsafe_allow_html=True
                )
                # Discount display
                if rule.value_type == ValueType.PERCENT and rule.value:
                    cb.metric("Discount", f"{rule.value}%")
                elif rule.value_type == ValueType.FIXED and rule.value:
                    cb.metric("Discount", f"₹{rule.value}")
                elif rule.value_type == ValueType.SPECIAL_PRICE and rule.special_price:
                    cb.metric("Sp. Price", f"₹{rule.special_price}")
                elif rule.value_type == ValueType.BOGO:
                    cb.metric("BOGO", f"B{rule.bogo_buy} G{rule.bogo_get}")
                elif rule.slab_config:
                    cb.metric("Slab", f"{len(rule.slab_config)} tiers")
                else:
                    cb.metric("Type", rule.value_type.value)

                cc.metric("GST", f"{rule.gst_rate}%")
                cd.metric("Priority", rule.priority)

                if rule.slab_config:
                    with st.expander("📊 Slab tiers"):
                        for s in rule.slab_config:
                            mx = f"–{s.max_qty}" if s.max_qty else "+"
                            st.markdown(f"- Qty {s.min_qty}{mx}: **{s.discount_pct}%**")

                if rule.conditions.promo_code:
                    st.markdown(
                        f'<span class="promo-badge">🎟️ Code: {rule.conditions.promo_code}</span>',
                        unsafe_allow_html=True
                    )
                conds = []
                if rule.conditions.party_tags:    conds.append(f"Tags: {rule.conditions.party_tags}")
                if rule.conditions.brand_groups:  conds.append(f"Brand: {rule.conditions.brand_groups}")
                if rule.conditions.product_cats:  conds.append(f"Cat: {rule.conditions.product_cats}")
                if rule.conditions.min_qty:        conds.append(f"Min qty: {rule.conditions.min_qty}")
                if rule.conditions.valid_to:       conds.append(f"Until: {rule.conditions.valid_to}")
                if conds:
                    st.caption(" · ".join(conds))


# ── TAB 2: SIMULATOR ────────────────────────────────────────────────────────
def _tab_simulator(rules):
    st.markdown("### 🧮 Discount Simulator")
    st.caption("Test how rules fire for any product / party combination.")

    c1, c2, c3 = st.columns(3)
    price    = c1.number_input("Base Price (₹)", value=1000.0, min_value=0.0, step=50.0)
    qty      = c2.number_input("Quantity", value=10, min_value=1, step=1)
    channel  = c3.selectbox("Channel", ["wholesale","retail","online","all"])

    c4, c5, c6 = st.columns(3)
    party_tags_raw = c4.text_input("Party Tags (comma)", "wholesale")
    brand_group    = c5.text_input("Brand Group", "")
    product_cat    = c6.text_input("Product Cat", "frame")
    promo_code     = st.text_input("Promo Code (optional)", "")
    cost_price     = st.number_input("Cost Price (₹, for margin)", value=0.0, min_value=0.0, step=50.0)

    party_tags = [t.strip() for t in party_tags_raw.split(",") if t.strip()]

    ch_map = {
        "wholesale": SalesChannel.WHOLESALE,
        "retail":    SalesChannel.RETAIL,
        "online":    SalesChannel.ONLINE,
        "all":       SalesChannel.ALL,
    }

    if st.button("▶ Run Simulation", type="primary"):
        engine = DiscountEngine(rules)
        item   = LineItem(
            base_price  = Decimal(str(price)),
            quantity    = int(qty),
            party_tags  = party_tags,
            brand_group = brand_group or None,
            product_cat = product_cat or None,
            channel     = ch_map.get(channel, SalesChannel.ALL),
            promo_code  = promo_code or None,
            cost_price  = Decimal(str(cost_price)) if cost_price else None,
        )
        sim = engine.simulate(item)

        # Winner
        w = sim["winner"]
        ms = w.get("margin_status","ok")
        ms_css = {"ok":"margin-ok","soft_warning":"margin-warn","hard_stop":"margin-stop"}.get(ms,"margin-ok")
        ms_label = {"ok":"✅ OK","soft_warning":"⚠️ Low Margin","hard_stop":"🛑 Hard Stop"}.get(ms,"✅ OK")

        st.markdown("#### 🏆 Winner")
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("Rule",     w["rule_applied"])
        m2.metric("Discount", f"{w['discount_pct']:.1f}% = ₹{w['discount_amount']:.2f}")
        m3.metric("Net",      f"₹{w['net_amount']:.2f}")
        m4.metric("GST",      f"₹{w['gst_amount']:.2f}")
        m5.metric("Payable",  f"₹{w['final_amount']:.2f}")

        if w.get("margin_pct") is not None:
            st.markdown(
                f'<span class="{ms_css}">Margin: {w["margin_pct"]:.1f}% — {ms_label}</span>',
                unsafe_allow_html=True
            )

        # All evaluated rules
        st.markdown("#### 📊 All Evaluated Rules")
        for ev in sim["all_evaluated"]:
            is_winner = ev.get("is_winner")
            prefix    = "🏆 " if is_winner else "   "
            css       = "winner-row" if is_winner else ""
            m_status  = ev.get("margin_status","ok")
            m_icon    = {"ok":"✅","soft_warning":"⚠️","hard_stop":"🛑"}.get(m_status,"")

            st.markdown(
                f'<div class="{css}">'
                f'{prefix}<b>{_icon(ev["rule_type"])} {ev["rule_name"]}</b> '
                f'(P{ev["priority"]}) — '
                f'Disc: <b>{ev["discount_pct"]:.1f}%</b> = ₹{ev["discount_amt"]:.2f} '
                f'→ Net ₹{ev["net_amount"]:.2f} | Final ₹{ev["final_amount"]:.2f} '
                f'{m_icon}'
                f'</div>',
                unsafe_allow_html=True
            )

        if not sim["all_evaluated"]:
            st.info("No rules matched this combination.")


# ── TAB 3: INVOICE BUILDER ───────────────────────────────────────────────────
def _tab_invoice_builder(rules):
    st.markdown("### 🧾 Invoice Builder")
    st.caption("Add multiple lines and preview totals with GST.")

    if "inv_lines" not in st.session_state:
        st.session_state.inv_lines = []

    ch_sel  = st.selectbox("Channel", ["wholesale","retail","online","all"], key="inv_ch")
    tags_in = st.text_input("Party Tags (comma)", "wholesale", key="inv_tags")
    tags    = [t.strip() for t in tags_in.split(",") if t.strip()]

    ch_map = {"wholesale":SalesChannel.WHOLESALE,"retail":SalesChannel.RETAIL,
               "online":SalesChannel.ONLINE,"all":SalesChannel.ALL}

    with st.expander("➕ Add Line"):
        la,lb,lc,ld = st.columns(4)
        ln_name  = la.text_input("Product",  "Frame A", key="ln_name")
        ln_price = lb.number_input("Price ₹", value=500.0, min_value=0.0, key="ln_price")
        ln_qty   = lc.number_input("Qty",     value=10, min_value=1, key="ln_qty")
        ln_gst   = ld.number_input("GST %",   value=12.0, min_value=0.0, key="ln_gst")
        ln_cat   = st.text_input("Product Cat", "frame", key="ln_cat")
        if st.button("Add Line", key="add_inv_line"):
            st.session_state.inv_lines.append({
                "name": ln_name, "price": ln_price,
                "qty": ln_qty,   "gst": ln_gst, "cat": ln_cat,
            })
            st.rerun()

    if not st.session_state.inv_lines:
        st.info("Add at least one line above.")
        return

    engine       = DiscountEngine(rules)
    total_gross  = 0.0
    total_disc   = 0.0
    total_gst    = 0.0
    total_final  = 0.0

    for i, ln in enumerate(st.session_state.inv_lines):
        item = LineItem(
            base_price  = Decimal(str(ln["price"])),
            quantity    = int(ln["qty"]),
            product_cat = ln["cat"] or None,
            party_tags  = tags,
            channel     = ch_map.get(ch_sel, SalesChannel.ALL),
            gst_rate    = Decimal(str(ln["gst"])),
        )
        res = engine.calculate(item)
        ca,cb,cc,cd,ce,cf = st.columns([3,1,1,1,1,1])
        ca.markdown(f"**{ln['name']}**")
        cb.metric("Gross", f"₹{float(res.gross_amount):.0f}")
        cc.metric("Rule",  res.rule_name[:12])
        cd.metric("Disc",  f"-₹{float(res.discount_amount):.0f}")
        ce.metric("GST",   f"₹{float(res.gst_amount):.0f}")
        cf.metric("Final", f"₹{float(res.final_amount):.0f}")

        total_gross += float(res.gross_amount)
        total_disc  += float(res.discount_amount)
        total_gst   += float(res.gst_amount)
        total_final += float(res.final_amount)

    st.divider()
    t1,t2,t3,t4 = st.columns(4)
    t1.metric("Total Gross",    f"₹{total_gross:.2f}")
    t2.metric("Total Discount", f"₹{total_disc:.2f}")
    t3.metric("Total GST",      f"₹{total_gst:.2f}")
    t4.metric("💰 Payable",    f"₹{total_final:.2f}")

    if st.button("🗑️ Clear Lines"):
        st.session_state.inv_lines = []
        st.rerun()


# ── TAB 4: OFFERS PANEL ──────────────────────────────────────────────────────
def _tab_offers_panel(rules):
    st.markdown("### 🏷️ Customer Offers Panel")
    st.caption("Offers visible to retail / online customers. Controlled by show_in_offers=True.")

    engine = DiscountEngine(rules)
    c1,c2,c3 = st.columns(3)
    price    = c1.number_input("Product Price ₹", value=800.0, min_value=0.0, key="op_price")
    qty      = c2.number_input("Quantity",         value=1, min_value=1, key="op_qty")
    cat      = c3.text_input("Product Cat",        "frame", key="op_cat")

    item = LineItem(
        base_price=Decimal(str(price)), quantity=int(qty),
        product_cat=cat or None, channel=SalesChannel.RETAIL,
    )
    offers = engine.list_available_offers(item)

    if not offers:
        st.info("No customer-facing offers match this product.")
        return

    for o in offers:
        with st.container(border=True):
            oa, ob = st.columns([5,1])
            oa.markdown(
                f"**{o['icon'] or ''} {o['display_label'] or o['name']}**  \n"
                f"<span style='color:#94a3b8;font-size:0.8rem'>"
                f"{o['discount_pct']:.0f}% off"
                f"{' · Code required' if o['requires_code'] else ''}"
                f"{'  · 🔗 Stackable' if o.get('stackable') else ''}"
                f"{'  · Expires '+str(o['valid_to'])[:10] if o.get('valid_to') else ''}"
                f"</span>",
                unsafe_allow_html=True
            )
            if o["requires_code"]:
                ob.markdown(
                    f'<span class="promo-badge">{o["promo_code"]}</span>',
                    unsafe_allow_html=True
                )


# ── TAB 5: ADD RULE ──────────────────────────────────────────────────────────
def _tab_add_rule():
    st.markdown("### ➕ Create Discount Rule")
    st.caption("Saves to `discount_rules` table via sql_adapter.")

    with st.form("add_rule_form"):
        r1, r2 = st.columns(2)
        name        = r1.text_input("Rule Name *")
        description = r2.text_input("Description")

        r3, r4, r5 = st.columns(3)
        rule_type   = r3.selectbox("Type *", [t.value for t in RuleType])
        value_type  = r4.selectbox("Value Type *", [v.value for v in ValueType])
        channel     = r5.selectbox("Channel *", ["wholesale","retail","online","all"])

        r6, r7, r8 = st.columns(3)
        value       = r6.number_input("Value (% or ₹)", value=0.0, min_value=0.0, step=0.5)
        gst_rate    = r7.number_input("GST Rate %", value=12.0, min_value=0.0, step=0.5)
        priority    = r8.number_input("Priority (1=highest)", value=3, min_value=1, max_value=5)

        st.markdown("**Conditions (all optional)**")
        c1, c2, c3 = st.columns(3)
        party_tags  = c1.text_input("Party Tags (comma)", "")
        brand_groups= c2.text_input("Brand Groups (comma)", "")
        product_cats= c3.text_input("Product Cats (comma)", "")

        c4, c5, c6 = st.columns(3)
        min_qty     = c4.number_input("Min Qty", value=0, min_value=0)
        valid_from  = c5.date_input("Valid From", value=None)
        valid_to    = c6.date_input("Valid To", value=None)

        # ── Phase 3C: Day-of-week + time window ──────────────────────────────
        st.markdown("**Schedule (optional — leave blank for always-on)**")
        sc1, sc2, sc3 = st.columns(3)
        active_days = sc1.multiselect(
            "Active Days",
            ["MON","TUE","WED","THU","FRI","SAT","SUN"],
            help="SAT + SUN = weekends only. Blank = every day."
        )
        time_from_add = sc2.text_input(
            "Start Time (HH:MM)", placeholder="18:00",
            help="Optional. Leave blank for all-day."
        )
        time_to_add = sc3.text_input(
            "End Time (HH:MM)", placeholder="22:00",
            help="Optional. Required if Start Time is set."
        )

        promo_code  = st.text_input("Promo Code (for promo_code type)")
        stackable   = st.checkbox("Stackable (accumulates with other rules)")
        show_offers = st.checkbox("Show in Customer Offers Panel")
        display_lbl = st.text_input("Display Label (for invoice)")
        icon        = st.text_input("Icon Emoji", "")

        submitted = st.form_submit_button("💾 Save Rule", type="primary")

        if submitted:
            if not name:
                st.error("Rule name is required.")
            else:
                rule_data = {
                    "name": name, "description": description,
                    "type": rule_type, "value_type": value_type,
                    "value": value if value > 0 else None,
                    "gst_rate": gst_rate, "priority": priority,
                    "channel": channel,
                    "promo_code": promo_code or None,
                    "stackable": stackable,
                    "show_in_offers": show_offers,
                    "display_label": display_lbl,
                    "icon_emoji": icon,
                    "active": True,
                    "conditions": {
                        "channel": channel,
                        **({"party_tags": [t.strip() for t in party_tags.split(",") if t.strip()]} if party_tags else {}),
                        **({"brand_groups": [b.strip() for b in brand_groups.split(",") if b.strip()]} if brand_groups else {}),
                        **({"product_cats": [c.strip() for c in product_cats.split(",") if c.strip()]} if product_cats else {}),
                        **({"min_qty": int(min_qty)} if min_qty > 0 else {}),
                        **({"valid_from": valid_from.isoformat()} if valid_from else {}),
                        **({"valid_to": valid_to.isoformat()} if valid_to else {}),
                        **({"promo_code": promo_code} if promo_code else {}),
                        **({"days": active_days} if active_days else {}),
                        **({"time_from": time_from_add.strip()} if time_from_add.strip() else {}),
                        **({"time_to": time_to_add.strip()} if time_to_add.strip() else {}),
                    },
                }
                if _save_rule_to_db(rule_data):
                    st.success("✅ Rule saved to database.")
                    try:
                        from modules.pricing.discount_engine import invalidate_rule_cache
                        invalidate_rule_cache()
                    except Exception:
                        pass
                    st.rerun()


# ── TAB 6: VALIDATOR TEST ────────────────────────────────────────────────────
def _tab_validator_test():
    st.markdown("### ✅ Validator Test")
    st.caption(
        "Test the 6 validators: ORDER · PARTY · PRODUCT · RX · FINANCIAL · TAX.  \n"
        "Edit the sample JSON or paste your own order dict."
    )

    sample = {
        "order_id":   "TEST-001",
        "party":      "Sample Opticals",
        "party_type": "WHOLESALE",
        "order_type": "WHOLESALE",
        "credit_limit": 50000,
        "outstanding":  10000,
        "lines": [
            {
                "product_id":   "prod-abc",
                "product_name": "Titan Frame T-1234",
                "eye_side":     "R",
                "billing_qty":  10,
                "unit_price":   800.0,
                "total_price":  8000.0,
                "billing_total":8000.0,
                "gst_percent":  12.0,
                "sph":   -2.50,
                "cyl":   -0.75,
                "axis":  90,
                "add_power": 0.0,
                "batch_allocation": [
                    {"batch_no":"B001","allocated_qty":10,"selling_price":800}
                ],
            }
        ],
    }

    raw = st.text_area(
        "Order JSON",
        value=json.dumps(sample, indent=2),
        height=300,
        key="val_test_json"
    )

    if st.button("▶ Run Validators", type="primary", key="run_validators"):
        try:
            order_data = json.loads(raw)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            return

        try:
            from modules.validation_gateway import validate_before_submit
            result = validate_before_submit(order_data)
        except Exception as e:
            st.error(f"Validator crashed: {e}")
            return

        # Summary
        if result["is_valid"]:
            st.success("✅ Order is VALID" + (" (with warnings)" if result["has_warnings"] else ""))
        else:
            st.error(f"❌ Order REJECTED — {len(result['errors'])} error(s)")

        if result["errors"]:
            for msg in result["errors"]:
                st.markdown(f'<div class="sev-error">❌ {msg}</div>', unsafe_allow_html=True)

        if result["warnings"]:
            for msg in result["warnings"]:
                st.markdown(f'<div class="sev-warning">⚠️ {msg}</div>', unsafe_allow_html=True)

        # Detail
        st.markdown("#### Validator Results")
        for r in result["results"]:
            icon = "✅" if r.get("passed") else ("⚠️" if r.get("severity") == "WARNING" else "❌")
            sev  = r.get("severity","INFO")
            _sev_css = "ok" if r.get("passed") else ("warning" if sev == "WARNING" else "error")
            st.markdown(
                f'<div class="sev-{_sev_css}">{icon} <b>{r.get("rule","?")}</b> [{sev}] — {r.get("message","")}</div>',
                unsafe_allow_html=True
            )


# ── MAIN ENTRY ───────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# PARTY DISCOUNT MANAGER — Tab 7
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_discount_schema_column():
    """
    Ensure all discount_rules columns exist.
    Runs migrations 002+003+custom safely — each uses IF NOT EXISTS.
    Safe to call on every page load; idempotent.
    """
    try:
        from modules.sql_adapter import run_write
        # Migration 002 columns
        for _col_sql in [
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'all'",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS stackable BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS display_label TEXT DEFAULT ''",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS icon_emoji TEXT DEFAULT ''",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS show_in_offers BOOLEAN NOT NULL DEFAULT FALSE",
            # Migration 003 columns
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'core'",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS conflict_strategy TEXT NOT NULL DEFAULT 'best_price'",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS parent_rule_id UUID",
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS conditions_dsl JSONB",
            # rule_type alias (some inserts use this)
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS rule_type TEXT",
            # Custom — schema grouping
            "ALTER TABLE discount_rules ADD COLUMN IF NOT EXISTS schema_name TEXT",
        ]:
            try:
                run_write(_col_sql)
            except Exception:
                pass  # column already exists with constraint — fine
    except Exception:
        pass


def _q_pd(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        import streamlit as st
        st.error(f"DB: {e}")
        return []


def _w_pd(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        import streamlit as st
        st.error(f"Write: {e}")
        return False


def _load_party_discount_rules(party_id: str) -> list:
    """Load all discount rules for a specific party."""
    return _q_pd("""
        SELECT id::text, name, type, value_type, value, special_price,
               conditions,
               COALESCE(conditions->>'channel', 'wholesale') AS channel,
               active, priority
        FROM discount_rules
        WHERE conditions @> %(cond_filter)s::jsonb
          AND COALESCE(active, TRUE) = TRUE
        ORDER BY priority, type, name
    """, {"cond_filter": __import__('json').dumps({"party_ids": [party_id]})})


def _save_brand_rule(party_id, party_name, brand, main_group, discount_pct, schema_name=None):
    """Upsert a brand-level discount rule."""
    _name = f"{party_name} | {brand} ({main_group}) | {discount_pct}%"
    _cond = {"party_ids": [party_id], "brand_groups": [brand], "channel": "wholesale"}
    import json
    # Check if rule already exists
    _exist = _q_pd("""
        SELECT id::text FROM discount_rules
        WHERE conditions @> %(cond_check)s::jsonb
          AND type = 'party'
          AND COALESCE(active, TRUE) = TRUE
        LIMIT 1
    """, {"cond_check": json.dumps({"party_ids": [party_id], "brand_groups": [brand]})})

    if _exist:
        return _w_pd("""
            UPDATE discount_rules
               SET value = %(val)s, name = %(nm)s, priority = 5,
                   schema_name = %(sn)s,
                   conditions = %(cond)s::jsonb, updated_at = NOW()
             WHERE id = %(id)s::uuid
        """, {"val": discount_pct, "nm": _name, "sn": schema_name,
               "cond": json.dumps(_cond), "id": _exist[0]["id"]})
    else:
        return _w_pd("""
            INSERT INTO discount_rules
                (name, type, value_type, value, priority, conditions,
                 active, schema_name, created_at, updated_at)
            VALUES
                (%(nm)s, 'party', 'percent', %(val)s, 5,
                 %(cond)s::jsonb, TRUE, %(sn)s, NOW(), NOW())
        """, {"nm": _name, "val": discount_pct, "sn": schema_name,
               "cond": json.dumps(_cond)})


def _save_product_rule(party_id, party_name, product_id, product_name, brand,
                       discount_pct=0.0, special_price=0.0, schema_name=None):
    """
    Upsert a product-level discount rule.
    special_price > 0 → saves as value_type=special_price, priority=1 (hard override).
    discount_pct  > 0 → saves as value_type=percent, priority=4.
    If both set, special_price wins.
    """
    import json
    _use_sp  = float(special_price or 0) > 0
    _use_pct = float(discount_pct  or 0) > 0 and not _use_sp
    if not _use_sp and not _use_pct:
        return True  # nothing to save

    _cond = {"party_ids": [party_id], "product_ids": [product_id], "channel": "wholesale"}

    if _use_sp:
        _name  = f"{party_name} | {product_name} | Special ₹{float(special_price):.0f}"
        _vtype = "special_price"
        _val   = None
        _spval = float(special_price)
        _pri   = 1
        _rtype = "special"
    else:
        _name  = f"{party_name} | {product_name} | {float(discount_pct):.1f}%"
        _vtype = "percent"
        _val   = float(discount_pct)
        _spval = None
        _pri   = 3
        _rtype = "product"

    _exist = _q_pd("""
        SELECT id::text, value_type FROM discount_rules
        WHERE conditions @> %(cond_check)s::jsonb
          AND type IN ('product', 'special')
          AND COALESCE(active, TRUE) = TRUE LIMIT 1
    """, {"cond_check": json.dumps({"party_ids": [party_id], "product_ids": [product_id]})})

    if _exist:
        return _w_pd("""
            UPDATE discount_rules
               SET value_type   = %(vt)s,
                   value        = %(val)s,
                   special_price = %(sp)s,
                   priority     = %(pri)s,
                   type         = %(rt)s,
                   name         = %(nm)s,
                   schema_name  = %(sn)s,
                   updated_at   = NOW()
             WHERE id = %(id)s::uuid
        """, {"vt": _vtype, "val": _val, "sp": _spval, "pri": _pri,
              "rt": _rtype, "nm": _name, "sn": schema_name,
              "id": _exist[0]["id"]})
    else:
        return _w_pd("""
            INSERT INTO discount_rules
                (name, type, value_type, value, special_price, priority,
                 conditions, active, schema_name, created_at, updated_at)
            VALUES
                (%(nm)s, %(rt)s, %(vt)s, %(val)s, %(sp)s, %(pri)s,
                 %(cond)s::jsonb, TRUE, %(sn)s, NOW(), NOW())
        """, {"nm": _name, "rt": _rtype, "vt": _vtype,
              "val": _val, "sp": _spval, "pri": _pri,
              "cond": json.dumps(_cond), "sn": schema_name})


def _save_sku_rule(party_id, party_name, product_id, product_name, brand,
                   sph, cyl, axis, add_power, special_price,
                   discount_pct=0.0, schema_name=None):
    """
    Upsert a SKU-level price rule.
    If special_price > 0 → saves as value_type=special_price (takes priority).
    Elif discount_pct > 0 → saves as value_type=percent.
    If both are 0 → no-op (nothing to save).
    """
    import json
    _pwr_str = f"SPH {sph:+.2f}" + (f" CYL {cyl:+.2f}" if cyl else "") + (f" AX {int(axis)}" if axis else "")

    # Determine what to save
    _use_sp  = float(special_price or 0) > 0
    _use_pct = float(discount_pct or 0) > 0 and not _use_sp

    if not _use_sp and not _use_pct:
        return True  # nothing to save — leave any existing rule as-is

    _cond = {"party_ids": [party_id], "product_ids": [product_id],
             "sph": sph, "cyl": cyl, "axis": axis}

    if _use_sp:
        _name     = f"{party_name} | {product_name} {_pwr_str} | Special ₹{float(special_price):.0f}"
        _vtype    = "special_price"
        _value    = None
        _sp_value = float(special_price)
    else:
        _name     = f"{party_name} | {product_name} {_pwr_str} | {float(discount_pct):.1f}%"
        _vtype    = "percent"
        _value    = float(discount_pct)
        _sp_value = None

    # Check for existing rule (either type) for this party+product+power
    _exist = _q_pd("""
        SELECT id::text, value_type FROM discount_rules
        WHERE conditions @> %(cond_check)s::jsonb
          AND type IN ('special', 'product')
          AND COALESCE(active, TRUE) = TRUE LIMIT 1
    """, {"cond_check": json.dumps({"party_ids": [party_id],
                                    "product_ids": [product_id],
                                    "sph": float(sph or 0)})})

    if _exist:
        return _w_pd("""
            UPDATE discount_rules
               SET value_type   = %(vt)s,
                   special_price = %(sp)s,
                   value        = %(val)s,
                   name         = %(nm)s,
                   schema_name  = %(sn)s,
                   updated_at   = NOW()
             WHERE id = %(id)s::uuid
        """, {"vt": _vtype, "sp": _sp_value, "val": _value,
              "nm": _name, "sn": schema_name, "id": _exist[0]["id"]})
    else:
        _sku_dsl = {"op": "all", "conditions": []}
        if sph is not None:
            _sku_dsl["conditions"].append({"field": "sph", "op": "eq", "value": float(sph)})
        if cyl is not None and abs(float(cyl)) > 0.01:
            _sku_dsl["conditions"].append({"field": "cyl", "op": "eq", "value": float(cyl)})
        if axis:
            _sku_dsl["conditions"].append({"field": "axis", "op": "eq", "value": int(axis)})

        _rule_type = "special" if _use_sp else "product"
        _priority  = 1 if _use_sp else 4

        return _w_pd("""
            INSERT INTO discount_rules
                (name, type, value_type, value, special_price, priority,
                 conditions, active, schema_name, created_at, updated_at)
            VALUES
                (%(nm)s, %(rt)s, %(vt)s, %(val)s, %(sp)s, %(pri)s,
                 %(cond)s::jsonb, TRUE, %(sn)s, NOW(), NOW())
        """, {"nm": _name, "rt": _rule_type, "vt": _vtype,
              "val": _value, "sp": _sp_value, "pri": _priority,
              "cond": json.dumps(_cond), "sn": schema_name})


def _tab_party_discount():
    """Tab 7: Party Discount Manager — Brand / Product / SKU level."""
    import streamlit as st
    import json

    _ensure_discount_schema_column()

    st.markdown("### 🏷️ Party Discount Manager")
    st.caption(
        "Set negotiated discounts per wholesale party — by brand, product, or individual SKU power. "
        "Save a discount set as a **Schema** to reuse across multiple parties."
    )

    _sub1, _sub2 = st.tabs(["👤 Party Rules", "📋 Schema Designer"])

    with _sub1:
        _tab_party_discount_rules()

    with _sub2:
        _tab_schema_designer()


def _tab_schema_designer():
    """
    Schema Designer — same UI as Party Rules but uses its own session keys (scd_ prefix)
    and a fixed schema placeholder party so pdm_party widget is never touched.
    """
    import streamlit as st
    import json

    _SCHEMA_PID   = "00000000-0000-0000-0000-000000000000"
    _SCHEMA_PNAME = "📋 [Schema Template]"

    st.caption(
        "Same interface as Party Rules — set brand, product and power discounts. "
        "Give a schema name and save. Apply to any party from the Party Rules tab."
    )

    # Ensure pseudo-party exists in DB
    try:
        from modules.sql_adapter import run_write as _rws
        _rws("""
            INSERT INTO parties (id, party_name, party_type, is_active)
            VALUES ('00000000-0000-0000-0000-000000000000'::uuid,
                    '📋 [Schema Template]', 'SCHEMA', TRUE)
            ON CONFLICT (id) DO NOTHING
        """)
    except Exception:
        pass

    # Reuse the full brand/product/SKU manager with scd_ prefixed keys
    _shared_discount_manager(
        sel_party_id   = _SCHEMA_PID,
        sel_party_name = _SCHEMA_PNAME,
        key_prefix     = "scd",
        schema_mode    = True,
    )



def _tab_party_discount_rules():
    """Wrapper — party selector then delegates to _render_discount_manager."""
    import streamlit as st
    _parties = _q_pd("""
        SELECT id::text AS id, party_name,
               COALESCE(party_type,'WHOLESALE') AS party_type
        FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN (
            'WHOLESALE','RETAILER','DEALER','DISTRIBUTOR',
            'OPTICIAN','DOCTOR','HOSPITAL','CLINIC',
            'CUSTOMER','TRADER','STOCKIST','AGENT',
            'SCHEMA'
        )
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY party_name
    """)
    if not _parties:
        st.warning("No wholesale parties found in DB.")
        return

    _party_opts = {p["id"]: f"{p['party_name']}  [{p['party_type']}]" for p in _parties}
    _party_ids  = list(_party_opts.keys())

    _pc1, _pc2 = st.columns([4, 2])
    with _pc1:
        _sel_party_id = st.selectbox(
            "Select Wholesale Party",
            _party_ids,
            format_func=lambda x: _party_opts.get(x, x),
            key="pdm_party"
        )
    _sel_party_name = next(p["party_name"] for p in _parties if p["id"] == _sel_party_id)

    # Clear stale session state from previous party selection
    _prev_pid_key = "pdm_prev_party_id"
    if st.session_state.get(_prev_pid_key) != _sel_party_id:
        # Party changed — clear ALL pdm_ widget state so page shows fresh
        _clear_keys = [k for k in list(st.session_state.keys())
                       if k.startswith("pdm_") and k != _prev_pid_key
                       and k != "pdm_party"]
        for _ck in _clear_keys:
            try: del st.session_state[_ck]
            except Exception: pass
        st.session_state[_prev_pid_key] = _sel_party_id

    with _pc2:
        # Show summary of existing rules for this party
        _existing = _load_party_discount_rules(_sel_party_id)
        _n_brand   = sum(1 for r in _existing if r.get("type") == "brand_group")
        _n_prod    = sum(1 for r in _existing if r.get("type") == "product")
        _n_sku     = sum(1 for r in _existing if r.get("type") == "special")
        st.markdown(
            f"<div style='background:#0d1b2a;border:1px solid #1e3a5f;border-radius:8px;"
            f"padding:10px 14px;margin-top:26px'>"
            f"<div style='color:#64748b;font-size:0.75rem'>Existing rules for this party</div>"
            f"<div style='color:#e2e8f0;font-size:0.85rem;margin-top:4px'>"
            f"🏭 {_n_brand} brand &nbsp;·&nbsp; 📦 {_n_prod} product &nbsp;·&nbsp; "
            f"⚡ {_n_sku} SKU</div></div>",
            unsafe_allow_html=True
        )

    st.markdown("---")
    _shared_discount_manager(
        sel_party_id   = _sel_party_id,
        sel_party_name = _sel_party_name,
        key_prefix     = "pdm",
        schema_mode    = False,
    )



def _shared_discount_manager(sel_party_id, sel_party_name,
                              key_prefix="pdm", schema_mode=False):
    """
    Core brand/product/SKU discount UI — shared between Party Rules and Schema Designer.
    key_prefix: "pdm" for party mode, "scd" for schema mode (prevents widget key collision).
    """
    import streamlit as st
    import json
    _sel_party_id   = sel_party_id
    _sel_party_name = sel_party_name
    _existing = _load_party_discount_rules(_sel_party_id)

    st.markdown("---")

    # ── STEP 2: Brand-level discounts ────────────────────────────────────────
    st.markdown("#### Step 2 — Brand Discounts")

    _brands_raw = _q_pd("""
        SELECT DISTINCT brand, main_group
        FROM products
        WHERE COALESCE(is_active, TRUE) = TRUE
          AND brand IS NOT NULL AND brand != ''
          AND main_group IS NOT NULL
        ORDER BY main_group, brand
    """)

    if not _brands_raw:
        st.warning("No brands found in products table.")
        return

    # Build existing rule lookup: (brand, '') -> rule
    # Rules saved via Tab 7 use type='party' with conditions.brand_groups=[brand]
    import json as _json_br
    _brand_rules = {}   # keyed by brand name only — one rule per brand regardless of main_group
    for _r in _existing:
        if _r.get("type") in ("party", "brand_group"):
            _cr = _r.get("conditions") or {}
            if isinstance(_cr, str):
                try: _cr = _json_br.loads(_cr)
                except: _cr = {}
            for _bg in (_cr.get("brand_groups") or []):
                _brand_rules[_bg] = _r   # key = brand string only

    # Group by main_group
    from collections import defaultdict as _dd
    _by_group = _dd(list)
    for b in _brands_raw:
        _by_group[b["main_group"]].append(b["brand"])

    # Select All checkbox
    _all_key = f"{key_prefix}_all_{_sel_party_id[:8]}"
    if _all_key not in st.session_state:
        st.session_state[_all_key] = False

    _sa_col, _sa_note = st.columns([1, 6])
    with _sa_col:
        _select_all = st.checkbox("☑ Select All Brands", key=_all_key)

    _brand_selections = {}   # (brand, main_group) → {enabled, discount_pct}

    for _mg, _brand_list in _by_group.items():
        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
            f"border-left:3px solid #3b82f6;border-radius:6px;"
            f"padding:5px 12px;margin:6px 0 4px'>"
            f"<span style='color:#3b82f6;font-weight:700;font-size:0.85rem'>"
            f"📂 {_mg}</span></div>",
            unsafe_allow_html=True
        )

        _header_cols = st.columns([0.4, 2.5, 1.5, 1.5, 1.5])
        _header_cols[0].markdown("<span style='color:#475569;font-size:0.72rem'>ON</span>",
                                  unsafe_allow_html=True)
        _header_cols[1].markdown("<span style='color:#475569;font-size:0.72rem'>Brand</span>",
                                  unsafe_allow_html=True)
        _header_cols[2].markdown("<span style='color:#475569;font-size:0.72rem'>Discount %</span>",
                                  unsafe_allow_html=True)
        _header_cols[3].markdown("<span style='color:#475569;font-size:0.72rem'>Current Rule</span>",
                                  unsafe_allow_html=True)
        _header_cols[4].markdown("<span style='color:#475569;font-size:0.72rem'>Action</span>",
                                  unsafe_allow_html=True)

        for _brand in sorted(_brand_list):
            _bkey = (_brand, _mg)
            _rule = _brand_rules.get(_brand)  # brand-only lookup — one rule covers all main_groups
            _has_rule = bool(_rule)
            _cur_pct  = float(_rule.get("value", 0)) if _rule else 0.0

            _bc0, _bc1, _bc2, _bc3, _bc4 = st.columns([0.4, 2.5, 1.5, 1.5, 1.5])
            _chk_key  = f"{key_prefix}_br_{_sel_party_id[:8]}_{_brand}_{_mg}".replace(" ","_")
            _pct_key  = f"{key_prefix}_pct_{_sel_party_id[:8]}_{_brand}_{_mg}".replace(" ","_")
            _enabled  = _bc0.checkbox("✓", key=_chk_key,
                                       value=_has_rule or _select_all,
                                       label_visibility="collapsed")
            _bc1.markdown(
                f"<div style='padding-top:6px;color:#e2e8f0;font-weight:600'>{_brand}</div>",
                unsafe_allow_html=True)
            _pct = _bc2.number_input("Discount %", min_value=0.0, max_value=100.0,
                                      value=_cur_pct, step=0.5, format="%.1f",
                                      key=_pct_key, label_visibility="collapsed")
            if _has_rule:
                _bc3.markdown(
                    f"<div style='padding-top:6px;font-size:0.75rem;"
                    f"color:#22c55e'>✅ {_cur_pct:.1f}%</div>",
                    unsafe_allow_html=True)
            else:
                _bc3.markdown(
                    "<div style='padding-top:6px;font-size:0.75rem;color:#64748b'>—</div>",
                    unsafe_allow_html=True)

            # Product-level drill down — use checkbox to avoid rerender resetting inputs
            _drill_key = f"{key_prefix}_drill_{_sel_party_id[:8]}_{_brand}_{_mg}".replace(" ","_")
            _expand_key = f"{key_prefix}_expand_{_brand}_{_mg}"
            _bc4.checkbox("📦 Products", key=_drill_key,
                          value=st.session_state.get(_expand_key, False),
                          label_visibility="visible")
            st.session_state[_expand_key] = st.session_state.get(_drill_key, False)

            # Auto-enable when % is entered
            _effective_enabled = _enabled or _pct > 0
            _brand_selections[_bkey] = {"enabled": _enabled, "effective_enabled": _effective_enabled, "pct": _pct}

            # ── PRODUCT LEVEL — Drill-down ────────────────────────────────
            if st.session_state.get(f"{key_prefix}_expand_{_brand}_{_mg}", False):
                _products = _q_pd("""
                    SELECT id::text AS product_id, product_name,
                           gst_percent, category
                    FROM products
                    WHERE brand = %(brand)s AND main_group = %(mg)s
                      AND COALESCE(is_active, TRUE) = TRUE
                    ORDER BY product_name
                """, {"brand": _brand, "mg": _mg})

                # Existing product rules for this party+brand
                import json as _json_pr
                _prod_rules    = {}   # product_id → percent rule
                _prod_sp_rules = {}   # product_id → special_price rule
                if _products:
                    for r in _existing:
                        if r.get("type") in ("product", "special"):
                            _cr = r.get("conditions") or {}
                            if isinstance(_cr, str):
                                try: _cr = _json_pr.loads(_cr)
                                except: _cr = {}
                            for _pid_r in (_cr.get("product_ids") or []):
                                if r.get("value_type") == "special_price":
                                    _prod_sp_rules[_pid_r] = r
                                else:
                                    _prod_rules[_pid_r] = r

                if not _products:
                    st.info(f"No products found for {_brand} / {_mg}")
                else:
                    with st.container():
                        st.markdown(
                            f"<div style='background:#071624;border:1px solid #1e3a5f;"
                            f"border-left:3px solid #8b5cf6;border-radius:6px;"
                            f"padding:8px 14px;margin:4px 0 6px 20px'>"
                            f"<span style='color:#8b5cf6;font-weight:700;font-size:0.8rem'>"
                            f"📦 Products — {_brand} / {_mg}</span>"
                            f"<span style='color:#475569;font-size:0.72rem;margin-left:8px'>"
                            f"Set % or special price ₹ per product — special price overrides %</span>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

                        # Same % for all toggle
                        _same_key = f"{key_prefix}_same_{_brand}_{_mg}_{_sel_party_id[:8]}".replace(" ","_")
                        _use_same = st.checkbox(
                            f"☑ Same as brand for all ({_pct:.1f}%)",
                            key=_same_key, value=False)

                        # ── Brand rule info bar ───────────────────────────────
                        if _pct > 0:
                            st.markdown(
                                f"<div style='background:#0a2a1a;border-left:3px solid #22c55e;"
                                f"border-radius:4px;padding:4px 12px;margin:2px 0 4px 0;"
                                f"font-size:0.74rem;color:#86efac'>"
                                f"🏷️ Brand rule active: <b>{_pct:.1f}%</b> off for "
                                f"<b>{_sel_party_name}</b> — "
                                f"product overrides below replace this for that product only"
                                f"</div>",
                                unsafe_allow_html=True)

                        # ── Column headers ────────────────────────────────────
                        _ph = st.columns([0.4, 2.5, 1.5, 1.5, 1.5, 1.5])
                        for _hl, _hc in zip(
                            ["", "Product", "Disc %", "Sp Price ₹", "Active Rule", "⚡ Powers"],
                            _ph
                        ):
                            _hc.markdown(
                                f"<span style='color:#475569;font-size:0.7rem'>{_hl}</span>",
                                unsafe_allow_html=True)

                        for _prod in _products:
                            _pid      = _prod["product_id"]
                            _pn       = _prod["product_name"]
                            _prule    = _prod_rules.get(_pid)         # percent rule
                            _sp_prule = _prod_sp_rules.get(_pid)      # special_price rule
                            _has_pr   = bool(_prule)
                            _has_sp_pr = bool(_sp_prule)
                            _cur_pr   = float(_prule.get("value", 0)) if _prule else 0.0
                            _cur_sp_pr = float(_sp_prule.get("special_price", 0)) if _sp_prule else 0.0

                            _pp0, _pp1, _pp2, _pp3, _pp4, _pp5 = st.columns([0.4, 2.5, 1.5, 1.5, 1.5, 1.5])
                            _p_chk_key = f"{key_prefix}_pchk_{_pid[:8]}_{_sel_party_id[:8]}"
                            _p_pct_key = f"{key_prefix}_ppct_{_pid[:8]}_{_sel_party_id[:8]}"
                            _p_sp_key  = f"{key_prefix}_psp_{_pid[:8]}_{_sel_party_id[:8]}"
                            _p_en = _pp0.checkbox("", key=_p_chk_key,
                                                   value=_has_pr or _has_sp_pr or _use_same,
                                                   label_visibility="collapsed")
                            _pp1.markdown(
                                f"<div style='padding-top:6px;color:#cbd5e1;font-size:0.82rem'>"
                                f"{_pn}</div>",
                                unsafe_allow_html=True)

                            # % input
                            _default_pct = _cur_pr if _has_pr else (_pct if _use_same else 0.0)
                            _p_pct = _pp2.number_input(
                                "Disc %", min_value=0.0, max_value=100.0,
                                value=_default_pct,
                                step=0.5, format="%.1f",
                                key=_p_pct_key,
                                label_visibility="collapsed",
                                disabled=not _p_en,
                                help="% discount on W/S price for this product")

                            # Special price ₹ input
                            _p_sp = _pp3.number_input(
                                "Sp Price ₹", min_value=0.0,
                                value=_cur_sp_pr,
                                step=5.0, format="%.0f",
                                key=_p_sp_key,
                                label_visibility="collapsed",
                                disabled=not _p_en,
                                help="Fixed special price ₹ for this product (overrides %). 0 = not set.")

                            # Status
                            if _has_sp_pr and _cur_sp_pr > 0:
                                _pp4.markdown(
                                    f"<div style='padding-top:6px;font-size:0.72rem;"
                                    f"color:#f59e0b'>⚡ ₹{_cur_sp_pr:,.0f}</div>",
                                    unsafe_allow_html=True)
                            elif _has_pr and _cur_pr > 0:
                                _pp4.markdown(
                                    f"<div style='padding-top:6px;font-size:0.72rem;"
                                    f"color:#22c55e'>✅ {_cur_pr:.1f}%</div>",
                                    unsafe_allow_html=True)
                            elif _pct > 0:
                                _pp4.markdown(
                                    f"<div style='padding-top:6px;font-size:0.72rem;"
                                    f"color:#64748b'>Brand {_pct:.1f}%</div>",
                                    unsafe_allow_html=True)
                            else:
                                _pp4.markdown(
                                    "<div style='padding-top:6px;font-size:0.72rem;"
                                    "color:#475569'>—</div>",
                                    unsafe_allow_html=True)

                            # SKU drill-down — checkbox so it stays open/closed reliably
                            _sku_drill_key = f"{key_prefix}_skud_{_pid[:8]}_{_sel_party_id[:8]}"
                            _show_powers = _pp5.checkbox(
                                "⚡ Powers", key=_sku_drill_key,
                                value=st.session_state.get(f"{key_prefix}_sku_{_pid}", False)
                            )
                            # Sync toggle state; clear SKU session data when collapsed
                            if _show_powers != st.session_state.get(f"{key_prefix}_sku_{_pid}", False):
                                st.session_state[f"{key_prefix}_sku_{_pid}"] = _show_powers
                                if not _show_powers:
                                    # Remove stale SKU input entries from session
                                    _stale = [k for k in st.session_state
                                              if k.startswith(f"{key_prefix}_sku_sel_{key_prefix}_sp_{_pid[:8]}")]
                                    for _sk in _stale:
                                        del st.session_state[_sk]

                            # Store product selection in session for save
                            # special_price takes priority over % when both set
                            st.session_state[f"{key_prefix}_sel_prod_{_pid}"] = {
                                "enabled":      _p_en,
                                "pct":          _p_pct,
                                "special_price": _p_sp,
                                "name":         _pn,
                                "brand":        _brand,
                                "product_id":   _pid
                            }

                            # ── SKU / POWER LEVEL ─────────────────────────
                            if st.session_state.get(f"{key_prefix}_sku_{_pid}", False):
                                _skus = _q_pd("""
                                    SELECT DISTINCT
                                           COALESCE(sph,0)       AS sph,
                                           COALESCE(cyl,0)           AS cyl,
                                           COALESCE(axis,0)          AS axis,
                                           COALESCE(add_power,0)     AS add_power,
                                           eye_side,
                                           MIN(selling_price)        AS selling_price,
                                           MIN(mrp)                  AS mrp,
                                           SUM(quantity)             AS qty
                                    FROM inventory_stock
                                    WHERE product_id = %(pid)s::uuid
                                      AND COALESCE(is_active, TRUE) = TRUE
                                      AND quantity > 0
                                    GROUP BY sph, cyl, axis, add_power, eye_side
                                    ORDER BY sph, cyl, axis
                                    LIMIT 80
                                """, {"pid": _pid})

                                # Existing SKU rules — special_price overrides product rule; product overrides brand
                                # Priority chain: Power SP (P1) > Power % (P4) > Product SP (P1) > Product % (P4) > Brand % (P3)
                                import json as _json_sk
                                _sku_rules = {}    # (sph,cyl,ax) -> special_price rule
                                _sku_pct_rules = {} # (sph,cyl,ax) -> percent rule
                                for r in _existing:
                                    if r.get("type") in ("special", "product"):
                                        _cr_sk = r.get("conditions") or {}
                                        if isinstance(_cr_sk, str):
                                            try: _cr_sk = _json_sk.loads(_cr_sk)
                                            except: _cr_sk = {}
                                        if _pid in (_cr_sk.get("product_ids") or []):
                                            _sph_k = float(_cr_sk.get("sph") or 0)
                                            _cyl_k = float(_cr_sk.get("cyl") or 0)
                                            _ax_k  = int(_cr_sk.get("axis") or 0)
                                            if r.get("value_type") == "special_price":
                                                _sku_rules[(_sph_k, _cyl_k, _ax_k)] = r
                                            elif r.get("value_type") == "percent":
                                                _sku_pct_rules[(_sph_k, _cyl_k, _ax_k)] = r

                                if not _skus:
                                    st.info(f"No stock found for {_pn}")
                                else:
                                    st.markdown(
                                        f"<div style='background:#040d1a;border:1px solid #1e3a5f;"
                                        f"border-left:3px solid #f59e0b;border-radius:6px;"
                                        f"padding:8px 14px;margin:4px 0 4px 40px'>"
                                        f"<span style='color:#f59e0b;font-weight:700;font-size:0.78rem'>"
                                        f"⚡ SKU Special Prices — {_pn}</span>"
                                        f"<span style='color:#475569;font-size:0.7rem;margin-left:8px'>"
                                        f"Enter special price or % discount where you want to override</span></div>",
                                        unsafe_allow_html=True
                                    )

                                    _sk_head = st.columns([0.4, 2, 1.2, 1, 1, 1.5, 1.5, 1.5, 1.5])
                                    for _sh, _sl in zip(
                                        ["", "Power", "Eye", "In Stock", "MRP ₹", "W/S ₹", "Special Price ₹", "Disc %", "Status"],
                                        _sk_head
                                    ):
                                        _sl.markdown(
                                            f"<span style='color:#475569;font-size:0.7rem'>{_sh}</span>",
                                            unsafe_allow_html=True)

                                    for _ski, _sku in enumerate(_skus):
                                        _sph  = float(_sku.get("sph", 0))
                                        _cyl  = float(_sku.get("cyl", 0))
                                        _ax   = int(_sku.get("axis", 0))
                                        _add  = float(_sku.get("add_power", 0))
                                        _eye  = str(_sku.get("eye_side","")).upper()
                                        _qty  = int(_sku.get("qty", 0))
                                        _mrp  = float(_sku.get("mrp", 0))
                                        _ws   = float(_sku.get("selling_price", 0))

                                        _pwr_str = f"SPH {_sph:+.2f}"
                                        if abs(_cyl) > 0.01:
                                            _pwr_str += f" CYL {_cyl:+.2f}"
                                        if _ax:
                                            _pwr_str += f" AX {_ax}"
                                        if _add > 0.01:
                                            _pwr_str += f" ADD +{_add:.2f}"

                                        _sku_rule     = _sku_rules.get((_sph, _cyl, _ax))
                                        _sku_pct_rule = _sku_pct_rules.get((_sph, _cyl, _ax))
                                        _has_sp  = bool(_sku_rule) and float(_sku_rule.get("special_price", 0) or 0) > 0
                                        _has_pct = bool(_sku_pct_rule) and float(_sku_pct_rule.get("value", 0) or 0) > 0
                                        _cur_sp  = float(_sku_rule.get("special_price", 0)) if _sku_rule else 0.0
                                        _cur_pct = float(_sku_pct_rule.get("value", 0)) if _sku_pct_rule else 0.0

                                        _skchk_key = f"{key_prefix}_skchk_{_pid[:8]}_{_sph}_{_cyl}_{_ax}_{_ski}"
                                        _skc0, _sk0, _sk1, _sk2, _sk3, _sk4, _sk5, _sk6, _sk7 = st.columns(
                                            [0.4, 2, 1.2, 1, 1, 1.5, 1.5, 1.5, 1.5])
                                        _sk_en = _skc0.checkbox("", key=_skchk_key,
                                                                  value=_has_sp or _has_pct,
                                                                  label_visibility="collapsed")
                                        _sk0.markdown(
                                            f"<div style='padding-top:5px;font-size:0.76rem;"
                                            f"color:#cbd5e1;font-family:monospace'>{_pwr_str}</div>",
                                            unsafe_allow_html=True)
                                        _sk1.markdown(
                                            f"<div style='padding-top:5px;font-size:0.76rem;"
                                            f"color:#94a3b8'>{_eye or 'Both'}</div>",
                                            unsafe_allow_html=True)
                                        _sk2.markdown(
                                            f"<div style='padding-top:5px;font-size:0.76rem;"
                                            f"color:#22c55e'>{_qty}</div>",
                                            unsafe_allow_html=True)
                                        _sk3.markdown(
                                            f"<div style='padding-top:5px;font-size:0.76rem;"
                                            f"color:#94a3b8'>₹{_mrp:,.0f}</div>",
                                            unsafe_allow_html=True)
                                        _sk4.markdown(
                                            f"<div style='padding-top:5px;font-size:0.76rem;"
                                            f"color:#94a3b8'>₹{_ws:,.0f}</div>",
                                            unsafe_allow_html=True)

                                        _sp_key  = f"{key_prefix}_sp_{_pid[:8]}_{_sph}_{_cyl}_{_ax}_{_sel_party_id[:8]}_{_ski}"
                                        _pct_key = f"{key_prefix}_pct_{_pid[:8]}_{_sph}_{_cyl}_{_ax}_{_sel_party_id[:8]}_{_ski}"

                                        _sp_val = _sk5.number_input(
                                            "Sp Price", min_value=0.0,
                                            value=_cur_sp, step=5.0, format="%.2f",
                                            key=_sp_key,
                                            label_visibility="collapsed",
                                            disabled=not _sk_en,
                                            help=f"Special price ₹ for {_pwr_str}. 0 = no override."
                                        )
                                        _pct_val = _sk6.number_input(
                                            "Disc %", min_value=0.0, max_value=100.0,
                                            value=_cur_pct, step=0.5, format="%.1f",
                                            key=_pct_key,
                                            label_visibility="collapsed",
                                            disabled=not _sk_en,
                                            help=f"% discount for {_pwr_str}. 0 = no override. Special price takes priority if both set."
                                        )

                                        # Status column — show active override
                                        if _has_sp and _cur_sp > 0:
                                            _sk7.markdown(
                                                f"<div style='padding-top:5px;font-size:0.72rem;"
                                                f"color:#f59e0b'>⚡ ₹{_cur_sp:,.0f}</div>",
                                                unsafe_allow_html=True)
                                        elif _has_pct and _cur_pct > 0:
                                            _sk7.markdown(
                                                f"<div style='padding-top:5px;font-size:0.72rem;"
                                                f"color:#22c55e'>✅ {_cur_pct:.1f}%</div>",
                                                unsafe_allow_html=True)
                                        else:
                                            _sk7.markdown(
                                                "<div style='padding-top:5px;font-size:0.72rem;"
                                                "color:#475569'>—</div>",
                                                unsafe_allow_html=True)

                                        # Store for save — special price takes priority over %
                                        st.session_state[f"{key_prefix}_sku_sel_{_sp_key}"] = {
                                            "product_id": _pid, "product_name": _pn,
                                            "brand": _brand, "sph": _sph,
                                            "cyl": _cyl, "axis": _ax, "add_power": _add,
                                            "special_price": _sp_val,
                                            "discount_pct": _pct_val,
                                        }

    # ── SAVE ALL RULES ────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Step 3 — Save Rules")

    _sv1, _sv2 = st.columns([3, 1])
    with _sv1:
        if schema_mode:
            _schema_name_input = st.text_input(
                "Schema Name *",
                placeholder="e.g.  Alcon Standard  |  Distributor Tier A  |  Premium Partner",
                key=f"{key_prefix}_schema_name_{_sel_party_id[:8]}"
            )
        else:
            _schema_name_input = ""   # party rules — no schema name needed
    with _sv2:
        st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        _do_save = st.button("💾 Save All Rules",
                             key=f"{key_prefix}_save_{_sel_party_id[:8]}",
                             type="primary", use_container_width=True)

    if _do_save:
        _schema = (_schema_name_input.strip() or None)
        _saved  = 0
        _errors = 0

        # ── Schema mode: require name + warn on duplicate ─────────────────────
        if schema_mode:
            if not _schema:
                st.error("⚠️ Schema name is required. Enter a name before saving.")
                st.stop()
            # Check if schema already exists
            _existing_schema = _q_pd("""
                SELECT schema_name, COUNT(*) AS rule_count
                FROM discount_rules
                WHERE schema_name = %(sn)s
                  AND COALESCE(active, TRUE) = TRUE
                GROUP BY schema_name
            """, {"sn": _schema})
            if _existing_schema:
                _esc = _existing_schema[0]
                _overwrite_key = f"{key_prefix}_overwrite_{_schema[:20]}".replace(" ","_")
                if not st.session_state.get(_overwrite_key):
                    st.warning(
                        f"⚠️ Schema **{_schema}** already exists with "
                        f"**{_esc['rule_count']} rule(s)**. "
                        f"Click Save again to overwrite, or change the schema name."
                    )
                    st.session_state[_overwrite_key] = True
                    st.stop()
                else:
                    # Confirmed overwrite — deactivate old rules first
                    _w_pd("""
                        UPDATE discount_rules SET active=FALSE, updated_at=NOW()
                        WHERE schema_name=%(sn)s
                    """, {"sn": _schema})
                    st.session_state.pop(_overwrite_key, None)

        # Save brand-level rules — save whenever pct > 0, checkbox not required
        for (_brand, _mg), _sel in _brand_selections.items():
            if float(_sel.get("pct") or 0) > 0:
                ok = _save_brand_rule(_sel_party_id, _sel_party_name,
                                       _brand, _mg, _sel["pct"], _schema)
                if ok: _saved += 1
                else:  _errors += 1

        # Save product-level rules from session state
        for _k, _v in st.session_state.items():
            if _k.startswith(f"{key_prefix}_sel_prod_") and isinstance(_v, dict):
                _p_has_pct = _v.get("enabled") and float(_v.get("pct", 0) or 0) > 0
                _p_has_sp  = _v.get("enabled") and float(_v.get("special_price", 0) or 0) > 0
                if _p_has_pct or _p_has_sp:
                    ok = _save_product_rule(
                        _sel_party_id, _sel_party_name,
                        _v["product_id"], _v["name"], _v["brand"],
                        _v.get("pct", 0),
                        _v.get("special_price", 0),
                        _schema)
                    if ok: _saved += 1
                    else:  _errors += 1

        # Save SKU special prices and % discounts
        for _k, _v in st.session_state.items():
            if _k.startswith(f"{key_prefix}_sku_sel_") and isinstance(_v, dict):
                _has_sp  = float(_v.get("special_price", 0) or 0) > 0
                _has_pct = float(_v.get("discount_pct",  0) or 0) > 0
                if _has_sp or _has_pct:
                    ok = _save_sku_rule(
                        _sel_party_id, _sel_party_name,
                        _v["product_id"], _v["product_name"], _v["brand"],
                        _v["sph"], _v["cyl"], _v["axis"], _v["add_power"],
                        _v.get("special_price", 0),
                        _v.get("discount_pct", 0),
                        _schema)
                    if ok: _saved += 1
                    else:  _errors += 1

        if _saved > 0:
            if schema_mode:
                st.success(
                    f"✅ Schema **{_schema}** saved with {_saved} rule(s).  \n"
                    f"Go to **Party Rules** tab → select a party → Apply Schema."
                )
                # Clear all scd_ widget state so page shows fresh for next schema
                _clear_scd = [k for k in list(st.session_state.keys())
                               if k.startswith("scd_") and "schema_name" not in k]
                for _ck in _clear_scd:
                    try: del st.session_state[_ck]
                    except Exception: pass
            else:
                st.success(f"✅ {_saved} rule(s) saved for **{_sel_party_name}**"
                           + (f" under schema **'{_schema}'**" if _schema else ""))
        if _errors > 0:
            st.warning(f"{_errors} rule(s) failed to save — check DB connection.")
        if _saved > 0:
            st.rerun()

    # ── Apply a saved schema to this party (party mode only) ─────────────────
    if schema_mode:
        return   # Schema Designer has no "apply" — that's done from Party Rules tab

    st.markdown("---")
    st.markdown("#### 🔁 Apply a Schema to This Party")
    st.caption("Copy a saved schema template and apply all its rules to the selected party.")

    _schemas_avail = _q_pd("""
        SELECT schema_name, COUNT(*) AS rule_count
        FROM discount_rules
        WHERE schema_name IS NOT NULL
          AND COALESCE(active, TRUE) = TRUE
        GROUP BY schema_name ORDER BY schema_name
    """)

    if not _schemas_avail:
        st.info("No schemas saved yet. Go to 📋 Schema Designer tab to create one.")
    else:
        _sch_options = {s["schema_name"]: f"{s['schema_name']} ({s['rule_count']} rules)"
                        for s in _schemas_avail}
        _sel_schema = st.selectbox(
            "Select Schema", list(_sch_options.keys()),
            format_func=lambda x: _sch_options[x],
            key=f"{key_prefix}_apply_schema_{_sel_party_id[:8]}"
        )

        # Preview schema rules
        _preview = _q_pd("""
            SELECT name, type, value_type,
                   COALESCE(value,0) AS value,
                   COALESCE(special_price,0) AS special_price
            FROM discount_rules
            WHERE schema_name = %(sn)s AND COALESCE(active,TRUE)=TRUE
            ORDER BY type, name
        """, {"sn": _sel_schema})
        with st.expander(f"👁 Preview {len(_preview)} rule(s) in this schema"):
            for _pr in _preview:
                _pname = str(_pr.get("name") or "")
                _pparts = _pname.split(" | ")
                _pclean = " | ".join(_pparts[1:]) if len(_pparts) > 1 else _pname
                _prval = (f"₹{float(_pr['special_price']):,.0f} special"
                          if _pr["value_type"] == "special_price"
                          else f"{float(_pr['value']):.1f}% off")
                _pico = {"party":"🏷️","product":"📦","special":"⚡"}.get(_pr["type"],"•")
                st.markdown(
                    f"<div style='font-size:0.76rem;padding:2px 8px;"
                    f"border-left:2px solid #334155;margin:2px 0'>"
                    f"{_pico} <b style='color:#e2e8f0'>{_pclean}</b> "
                    f"<span style='color:#22c55e'>{_prval}</span></div>",
                    unsafe_allow_html=True)

        _ac1, _ac2 = st.columns([3, 1])
        _ac2.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        if _ac2.button(
            f"Apply to {_sel_party_name[:20]}",
            key=f"{key_prefix}_do_apply_{_sel_schema[:12]}_{_sel_party_id[:8]}".replace(" ","_"),
            type="primary", use_container_width=True
        ):
            _src = _q_pd("""
                SELECT name, type, value_type, value, special_price, priority, conditions
                FROM discount_rules
                WHERE schema_name=%(sn)s AND COALESCE(active,TRUE)=TRUE
            """, {"sn": _sel_schema})

            _SCHEMA_PID = "00000000-0000-0000-0000-000000000000"
            import json as _jap

            # Deactivate existing rules for this party+schema (prevents duplicates on re-apply)
            _w_pd("""
                UPDATE discount_rules SET active=FALSE, updated_at=NOW()
                WHERE schema_name=%(sn)s
                  AND conditions->'party_ids' @> %(pid_check)s::jsonb
                  AND conditions->'party_ids' != %(schema_pid)s::jsonb
            """, {
                "sn":         _sel_schema,
                "pid_check":  _jap.dumps([_sel_party_id]),
                "schema_pid": _jap.dumps([_SCHEMA_PID]),
            })

            _applied = 0
            for _sr in _src:
                _cond = _sr.get("conditions") or {}
                if isinstance(_cond, str):
                    try: _cond = _jap.loads(_cond)
                    except: _cond = {}
                if _cond.get("party_ids") in ([_SCHEMA_PID], []):
                    _cond["party_ids"] = [_sel_party_id]
                _new_nm = f"{_sel_party_name} | " + _sr["name"].split(" | ", 1)[-1]
                _w_pd("""
                    INSERT INTO discount_rules
                        (name,type,value_type,value,special_price,priority,
                         conditions,active,schema_name,created_at,updated_at)
                    VALUES(%(nm)s,%(tp)s,%(vt)s,%(val)s,%(sp)s,%(pri)s,
                           %(cond)s::jsonb,TRUE,%(sn)s,NOW(),NOW())
                """, {
                    "nm":_new_nm,"tp":_sr["type"],"vt":_sr["value_type"],
                    "val":_sr.get("value"),"sp":_sr.get("special_price"),
                    "pri":_sr.get("priority",3),"cond":_jap.dumps(_cond),
                    "sn":_sel_schema,
                })
                _applied += 1

            st.success(f"✅ **{_sel_schema}** applied to **{_sel_party_name}** — {_applied} rules.")
            try:
                from modules.pricing.discount_engine import invalidate_rule_cache
                invalidate_rule_cache()
            except Exception: pass
            st.rerun()



def render_pricing_admin():
    st.markdown(_CSS, unsafe_allow_html=True)
    _render_sidebar()

    st.title("💲 Pricing & Discount Admin")
    st.caption("Manage discount rules, simulate pricing, test validators.")
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11, tab12, tab13 = st.tabs([
        "📋 Active Rules",
        "🧮 Simulator",
        "🧾 Invoice Builder",
        "🏷️ Offers Panel",
        "➕ Add Rule",
        "✅ Validator Test",
        "🏷️ Party Discounts",
        "✨ Coating Upgrades",
        "📊 Analytics",
        "⏰ Scheduled Offers",
        "🎯 Pricing Tiers",
        "🤖 AI Suggestions",
        "🤝 Club Offers",
    ])

    rules = _load_rules_from_db()

    with tab1: _tab_active_rules(rules)
    with tab2: _tab_simulator(rules)
    with tab3: _tab_invoice_builder(rules)
    with tab4: _tab_offers_panel(rules)
    with tab5: _tab_add_rule()
    with tab6: _tab_validator_test()
    with tab7: _tab_party_discount()
    with tab8: _tab_coating_upgrades()
    with tab9:  _tab_analytics()
    with tab10: _tab_scheduled_offers(rules)
    with tab11: _tab_pricing_tiers()
    with tab12: _tab_ai_suggestions()
    with tab13: _tab_club_offers()


# ── TAB 8: COATING UPGRADES ──────────────────────────────────────────────────
def _tab_coating_upgrades():
    """
    Coating Upgrade Rules — "Get AR coating at same price as basic lens"

    Three upgrade mechanisms:
      1. FREE UPGRADE   — customer selects AR lens, billed at basic lens price
                          (special_price = base_lens_price on AR coating product)
      2. FREE COATING   — coating is added at ₹0 (100% discount on coating line)
      3. PRICE MATCH    — specific AR product billed at a capped special price

    All three are implemented via discount_rules (type=coating) and fire through
    the same engine._apply_to_line path as all other rules.
    No new engine code needed — uses existing SPECIAL_PRICE and PERCENT value_types.
    """
    st.markdown("### ✨ Coating Upgrade Rules")
    st.caption(
        "Define rules that give customers a better coating at no extra charge — "
        "or at the price of a basic lens. Works for AR, Blue Cut, Photochromic, etc."
    )

    # ── Existing coating rules ────────────────────────────────────────────────
    st.markdown("#### Active Coating Rules")
    try:
        coating_rules = _q("""
            SELECT id::text, name, value_type, value, special_price,
                   conditions, active, priority, stackable,
                   COALESCE(display_label, '') AS display_label
            FROM discount_rules
            WHERE type = 'coating'
              AND COALESCE(active, TRUE) = TRUE
            ORDER BY priority, name
        """) or []
    except Exception as _e:
        coating_rules = []
        st.warning(f"Could not load coating rules: {_e}")

    if not coating_rules:
        st.info("No coating upgrade rules defined yet. Create one below.")
    else:
        for cr in coating_rules:
            cond = cr.get("conditions") or {}
            if isinstance(cond, str):
                import json
                try: cond = json.loads(cond)
                except Exception: cond = {}

            vtype = cr.get("value_type", "")
            if vtype == "special_price":
                disc_display = f"Special price ₹{cr.get('special_price') or 0:.2f}"
            elif vtype == "percent":
                disc_display = f"{cr.get('value') or 0:.0f}% off"
            elif vtype == "fixed":
                disc_display = f"₹{cr.get('value') or 0:.2f} off"
            else:
                disc_display = vtype

            cats     = cond.get("product_cats") or []
            products = cond.get("product_ids") or []
            parties  = cond.get("party_tags") or []

            col_a, col_b, col_c = st.columns([3, 2, 1])
            col_a.markdown(f"**✨ {cr['name']}**  \n`{disc_display}`")
            col_b.caption(
                f"Applies to: {', '.join(cats) or ', '.join(products[:2]) or 'all products'}  \n"
                f"Parties: {', '.join(parties) or 'all'}  \n"
                f"Stackable: {'Yes' if cr.get('stackable') else 'No'}"
            )
            with col_c:
                if st.button("🗑️ Deactivate", key=f"del_coat_{cr['id']}"):
                    try:
                        from modules.sql_adapter import run_write
                        run_write(
                            "UPDATE discount_rules SET active=FALSE, updated_at=NOW() "
                            "WHERE id=%s::uuid", (cr["id"],)
                        )
                        from modules.pricing.discount_engine import invalidate_rule_cache
                        invalidate_rule_cache()
                        st.success("Rule deactivated.")
                        st.rerun()
                    except Exception as _de:
                        st.error(f"Failed: {_de}")
        st.markdown("---")

    # ── Create new coating upgrade rule ──────────────────────────────────────
    st.markdown("#### Create Coating Upgrade Rule")

    with st.form("coating_upgrade_form", clear_on_submit=True):
        st.markdown("**Rule Details**")
        c1, c2 = st.columns(2)
        rule_name   = c1.text_input("Rule Name *",
                                     placeholder="AR Coating Free Upgrade — Wholesale")
        description = c2.text_input("Description",
                                     placeholder="Get AR coating at price of basic lens")

        st.markdown("**Upgrade Mechanism**")
        mechanism = st.radio(
            "How should the upgrade work?",
            options=[
                "special_price — Bill coating at a fixed special price (e.g. same as basic lens)",
                "percent       — Percentage discount on the coating product (e.g. 100% = free)",
                "fixed         — Fixed ₹ discount on the coating (e.g. ₹200 off AR)",
            ],
            index=0,
        )

        m1, m2 = st.columns(2)
        special_price_val = m1.number_input(
            "Special Price ₹ (for special_price mechanism)",
            value=0.0, min_value=0.0, step=10.0,
            help="The price the coating will be billed at, e.g. 500 = same as basic lens"
        )
        discount_val = m2.number_input(
            "Discount % or ₹ (for percent/fixed mechanism)",
            value=0.0, min_value=0.0, step=5.0,
            help="100% = free coating. Or ₹200 = ₹200 off coating price"
        )

        st.markdown("**Applicability**")
        a1, a2, a3 = st.columns(3)
        product_cats = a1.text_input(
            "Product Categories (comma-separated)",
            placeholder="ar_coating, blue_cut",
            help="Which coating categories this rule applies to"
        )
        party_tags = a2.text_input(
            "Party Tags (comma-separated)",
            placeholder="wholesale, vip",
            help="Leave blank = applies to all parties"
        )
        channel = a3.selectbox(
            "Channel", ["all", "wholesale", "retail", "online"], index=0
        )

        b1, b2, b3 = st.columns(3)
        gst_rate   = b1.number_input("GST Rate %", value=18.0, min_value=0.0, step=1.0)
        priority   = b2.number_input("Priority (1=highest)", value=3, min_value=1, max_value=5)
        stackable  = b3.checkbox(
            "Stackable (stacks on top of party/product discount)",
            value=True,
            help="Tick = coating upgrade stacks with party 10% etc. "
                 "Untick = coating upgrade replaces other discounts on this line."
        )

        display_lbl = st.text_input(
            "Display Label (shown on cart and invoice)",
            placeholder="✨ AR Upgrade Free",
        )

        submitted = st.form_submit_button("💾 Save Coating Rule", type="primary")

        if submitted:
            if not rule_name:
                st.error("Rule name is required.")
            else:
                # Determine value_type and value from mechanism selection
                if "special_price" in mechanism:
                    vtype    = "special_price"
                    val      = None
                    sp_val   = special_price_val if special_price_val > 0 else None
                elif "percent" in mechanism:
                    vtype    = "percent"
                    val      = discount_val if discount_val > 0 else None
                    sp_val   = None
                else:
                    vtype    = "fixed"
                    val      = discount_val if discount_val > 0 else None
                    sp_val   = None

                if vtype == "special_price" and not sp_val:
                    st.error("Enter a special price greater than 0.")
                elif vtype in ("percent", "fixed") and not val:
                    st.error("Enter a discount value greater than 0.")
                else:
                    cats_list    = [c.strip() for c in product_cats.split(",") if c.strip()]
                    parties_list = [p.strip() for p in party_tags.split(",") if p.strip()]

                    conditions = {"channel": channel}
                    if cats_list:
                        conditions["product_cats"] = cats_list
                    if parties_list:
                        conditions["party_tags"] = parties_list

                    rule_data = {
                        "name":          rule_name,
                        "description":   description,
                        "type":          "coating",
                        "value_type":    vtype,
                        "value":         val,
                        "special_price": sp_val,
                        "gst_rate":      gst_rate,
                        "priority":      priority,
                        "stackable":     stackable,
                        "display_label": display_lbl or rule_name,
                        "icon_emoji":    "✨",
                        "show_in_offers": True,
                        "conditions":    conditions,
                        "conflict_strategy": "best_price",
                    }

                    if _save_rule_to_db(rule_data):
                        try:
                            from modules.pricing.discount_engine import invalidate_rule_cache
                            invalidate_rule_cache()
                        except Exception:
                            pass
                        st.success(
                            f"✅ Coating upgrade rule **{rule_name}** saved. "
                            f"It will fire on the next order for products in: "
                            f"{', '.join(cats_list) or 'all categories'}"
                        )
                        st.rerun()

    # ── How it works explanation ──────────────────────────────────────────────
    with st.expander("📖 How Coating Upgrades Work", expanded=False):
        st.markdown("""
**What this does**

When a customer selects a lens with an AR/Blue-cut/Photochromic coating, the upgrade
rule fires automatically and adjusts the price — without the operator doing anything.

---

**Mechanism 1 — Special Price (most common)**

> Customer wants AR coating lens (normally ₹800).
> Basic lens price is ₹500.
> Rule: `special_price = 500` on product_cats `ar_coating`

Result: AR lens billed at ₹500. Customer gets better coating, pays basic price.

---

**Mechanism 2 — Percent Discount**

> AR coating lens costs ₹800.
> Rule: `100%` discount on product_cats `ar_coating`

Result: AR coating is free. Use `50%` for half-price upgrade.

---

**Mechanism 3 — Fixed Discount**

> AR coating lens costs ₹800. You want to give ₹200 off.
> Rule: `fixed ₹200` discount on product_cats `ar_coating`

Result: AR coating billed at ₹600.

---

**Stackable flag**

If ticked, the coating upgrade stacks on top of the party/product discount on the same line.
Example: Party gets 10% off + AR upgrade at special price.

If unticked, the coating rule is the only rule that fires on that line (best-wins).

---

**DB column `product_cats` matches `products.main_group`**

So if your products table has `main_group = 'ar_coating'`, set Product Categories
to `ar_coating` in this form.
        """)


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3A — DISCOUNT ANALYTICS DASHBOARD
# ════════════════════════════════════════════════════════════════════════════
def _tab_analytics():
    """
    Phase 3A — Discount Analytics Dashboard (10/10 Production Grade).
    Delegates to modules/analytics/discount_dashboard.py.
    """
    try:
        from modules.analytics.discount_dashboard import render_discount_dashboard
        render_discount_dashboard()
        return
    except ImportError:
        pass
    # Fallback: try from deploy folder directly
    try:
        import importlib, sys, os
        _dp = os.path.join(os.path.dirname(__file__), "discount_dashboard.py")
        if os.path.exists(_dp):
            import importlib.util
            spec = importlib.util.spec_from_file_location("dd", _dp)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.render_discount_dashboard()
            return
    except Exception:
        pass
    # Last resort: inline version
    """
    Phase 3A — Discount Analytics Dashboard.
    Queries order_lines (already populated by Phase 2A) for:
      - Total discount given away (leakage)
      - Rule usage frequency
      - Margin distribution
      - Top discounted products/parties
    All read-only SQL. No changes to engine or data.
    """
    import json
    st.markdown("### 📊 Discount Analytics Dashboard")
    st.caption("Live data from order_lines — populated automatically since Phase 2A deploy.")

    # ── Period selector ───────────────────────────────────────────────────────
    period = st.selectbox(
        "Period", [7, 30, 60, 90, 180],
        format_func=lambda x: f"Last {x} days", index=1, key="analytics_period"
    )

    try:
        from modules.sql_adapter import run_query

        # ── KPI row ───────────────────────────────────────────────────────────
        kpis = run_query("""
            SELECT
                COUNT(DISTINCT o.id)                                    AS total_orders,
                COALESCE(SUM(ol.discount_amount), 0)                    AS total_discount_given,
                COALESCE(SUM(ol.unit_price * ol.quantity), 0)           AS total_gross,
                COALESCE(AVG(NULLIF(ol.discount_percent, 0)), 0)        AS avg_discount_pct,
                COUNT(CASE WHEN ol.margin_status = 'soft_warning' THEN 1 END) AS soft_warn_lines,
                COUNT(CASE WHEN ol.margin_status = 'hard_stop'    THEN 1 END) AS hard_stop_lines
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - INTERVAL '%s days'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, (period,)) or [{}]
        kpi = kpis[0]

        gross          = float(kpi.get("total_gross") or 0)
        disc_given     = float(kpi.get("total_discount_given") or 0)
        leakage_pct    = round(disc_given / gross * 100, 2) if gross > 0 else 0.0

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Orders",          int(kpi.get("total_orders") or 0))
        k2.metric("Gross Revenue",   f"₹{gross:,.0f}")
        k3.metric("Discount Leakage",f"₹{disc_given:,.0f}",
                  delta=f"{leakage_pct:.1f}% of gross",
                  delta_color="inverse")
        k4.metric("Avg Discount %",  f"{float(kpi.get('avg_discount_pct') or 0):.1f}%")
        k5.metric("Margin Warnings", int(kpi.get("soft_warn_lines") or 0),
                  delta=f"{int(kpi.get('hard_stop_lines') or 0)} hard stops",
                  delta_color="inverse")

        st.markdown("---")

        col_l, col_r = st.columns(2)

        # ── Rule usage frequency ──────────────────────────────────────────────
        with col_l:
            st.markdown("#### 🏷️ Rule Usage (Top 10)")
            usage = run_query("""
                SELECT
                    COALESCE(ol.discount_rule, 'No Discount') AS rule_name,
                    COUNT(*)                                  AS line_count,
                    COALESCE(SUM(ol.discount_amount), 0)      AS total_discount
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE o.created_at >= NOW() - INTERVAL '%s days'
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND ol.discount_rule IS NOT NULL
                  AND ol.discount_rule != ''
                GROUP BY ol.discount_rule
                ORDER BY line_count DESC
                LIMIT 10
            """, (period,)) or []

            if usage:
                import pandas as pd
                df_u = pd.DataFrame(usage)
                df_u["total_discount"] = df_u["total_discount"].apply(
                    lambda x: f"₹{float(x):,.0f}")
                st.dataframe(
                    df_u.rename(columns={
                        "rule_name": "Rule", "line_count": "Lines Used",
                        "total_discount": "Discount Given"
                    }),
                    use_container_width=True, hide_index=True
                )
            else:
                st.info("No discount rule data yet for this period.")

        # ── Margin distribution ───────────────────────────────────────────────
        with col_r:
            st.markdown("#### 📉 Margin Distribution")
            margin_dist = run_query("""
                SELECT
                    CASE
                        WHEN ol.margin_status = 'hard_stop'    THEN '🛑 Hard Stop (<5%)'
                        WHEN ol.margin_status = 'soft_warning' THEN '⚠️ Low (5–15%)'
                        WHEN ol.margin_status = 'ok'           THEN '✅ Healthy (>15%)'
                        ELSE '— No margin data'
                    END AS status_label,
                    COUNT(*) AS lines
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE o.created_at >= NOW() - INTERVAL '%s days'
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                GROUP BY ol.margin_status
                ORDER BY lines DESC
            """, (period,)) or []

            if margin_dist:
                import pandas as pd
                df_m = pd.DataFrame(margin_dist)
                st.dataframe(
                    df_m.rename(columns={"status_label": "Margin Band", "lines": "Lines"}),
                    use_container_width=True, hide_index=True
                )
            else:
                st.info("No margin data yet. Ensure purchase_rate is set on products.")

        st.markdown("---")

        # ── Top discounted products ───────────────────────────────────────────
        st.markdown("#### 📦 Top 10 Products by Discount Given")
        top_products = run_query("""
            SELECT
                COALESCE(p.product_name, 'Unknown')         AS product,
                COALESCE(p.brand, '')                       AS brand,
                COALESCE(p.main_group, '')                  AS category,
                COUNT(ol.id)                                AS lines,
                COALESCE(SUM(ol.discount_amount), 0)        AS total_discount,
                COALESCE(AVG(ol.discount_percent), 0)       AS avg_disc_pct
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE o.created_at >= NOW() - INTERVAL '%s days'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.discount_amount, 0) > 0
            GROUP BY p.product_name, p.brand, p.main_group
            ORDER BY total_discount DESC
            LIMIT 10
        """, (period,)) or []

        if top_products:
            import pandas as pd
            df_p = pd.DataFrame(top_products)
            df_p["total_discount"] = df_p["total_discount"].apply(
                lambda x: f"₹{float(x):,.0f}")
            df_p["avg_disc_pct"] = df_p["avg_disc_pct"].apply(
                lambda x: f"{float(x):.1f}%")
            st.dataframe(
                df_p.rename(columns={
                    "product": "Product", "brand": "Brand",
                    "category": "Category", "lines": "Lines",
                    "total_discount": "Discount Given",
                    "avg_disc_pct": "Avg Disc %"
                }),
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No discounted orders found for this period.")

        # ── Top discounted parties ────────────────────────────────────────────
        st.markdown("#### 👤 Top 10 Parties by Discount Received")
        top_parties = run_query("""
            SELECT
                o.party_name,
                o.order_type,
                COUNT(DISTINCT o.id)                        AS orders,
                COALESCE(SUM(ol.discount_amount), 0)        AS total_discount,
                COALESCE(SUM(ol.unit_price * ol.quantity), 0) AS gross_value
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            WHERE o.created_at >= NOW() - INTERVAL '%s days'
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.discount_amount, 0) > 0
            GROUP BY o.party_name, o.order_type
            ORDER BY total_discount DESC
            LIMIT 10
        """, (period,)) or []

        if top_parties:
            import pandas as pd
            df_pa = pd.DataFrame(top_parties)
            df_pa["total_discount"] = df_pa["total_discount"].apply(
                lambda x: f"₹{float(x):,.0f}")
            df_pa["gross_value"] = df_pa["gross_value"].apply(
                lambda x: f"₹{float(x):,.0f}")
            st.dataframe(
                df_pa.rename(columns={
                    "party_name": "Party", "order_type": "Type",
                    "orders": "Orders", "total_discount": "Discount Received",
                    "gross_value": "Gross Value"
                }),
                use_container_width=True, hide_index=True
            )

    except Exception as e:
        st.error(f"Analytics query failed: {e}")
        st.caption("Ensure Phase 2A migration has run and order_lines has discount columns.")


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3C — SCHEDULED OFFERS
# (listed before 3B as per PDF execution order: Analytics → Scheduled → Tiers → AI)
# ════════════════════════════════════════════════════════════════════════════
def _tab_scheduled_offers(rules):
    """
    Phase 3C — Scheduled Offers view.
    valid_from / valid_to already supported by the frozen engine (filter_rules
    checks dates on every call). This tab surfaces the schedule so operators
    can see what's active, upcoming, and expiring — no new engine code needed.
    """
    from datetime import date, timedelta
    st.markdown("### ⏰ Scheduled Offers")
    st.caption(
        "Time-based rules are handled automatically by the engine — it checks "
        "valid_from / valid_to on every order. Use the Add Rule tab to set dates."
    )

    today = date.today()

    try:
        from modules.sql_adapter import run_query
        sched = run_query("""
            SELECT
                id::text, name, type, value_type, value, special_price,
                conditions,
                COALESCE(active, TRUE) AS active,
                COALESCE(display_label, name) AS label,
                COALESCE(icon_emoji, '') AS icon
            FROM discount_rules
            WHERE (conditions->>'valid_from' IS NOT NULL
                   OR conditions->>'valid_to' IS NOT NULL)
              AND COALESCE(active, TRUE) = TRUE
            ORDER BY (conditions->>'valid_from')::date ASC NULLS LAST
        """) or []
    except Exception as _e:
        sched = []
        st.warning(f"Could not load scheduled offers: {_e}")

    if not sched:
        st.info(
            "No scheduled offers defined yet.  \n"
            "Go to **➕ Add Rule** and fill in the **Valid From / Valid To** fields "
            "to create a timed offer (Diwali, weekend discount, seasonal sale, etc.)"
        )
    else:
        active_now, upcoming, expired = [], [], []

        for r in sched:
            import json
            cond = r.get("conditions") or {}
            if isinstance(cond, str):
                try: cond = json.loads(cond)
                except Exception: cond = {}

            vf_str = cond.get("valid_from")
            vt_str = cond.get("valid_to")

            vf = date.fromisoformat(vf_str) if vf_str else None
            vt = date.fromisoformat(vt_str) if vt_str else None

            r["_vf"] = vf
            r["_vt"] = vt

            if (vf is None or vf <= today) and (vt is None or vt >= today):
                active_now.append(r)
            elif vf and vf > today:
                upcoming.append(r)
            else:
                expired.append(r)

        def _render_offer_row(r, badge):
            vf = r["_vf"]
            vt = r["_vt"]
            vtype = r.get("value_type", "")
            if vtype == "percent":
                disc = f"{float(r.get('value') or 0):.0f}%"
            elif vtype == "special_price":
                disc = f"Special ₹{r.get('special_price') or 0}"
            elif vtype == "fixed":
                disc = f"₹{float(r.get('value') or 0):.0f} off"
            else:
                disc = vtype
            dates = f"{vf.strftime('%d %b') if vf else '∞'} → {vt.strftime('%d %b %Y') if vt else '∞'}"
            days_left = (vt - today).days if vt else None
            col1, col2, col3 = st.columns([3, 2, 2])
            col1.markdown(f"{badge} **{r.get('icon','')} {r['name']}**  \n`{disc}` — {r['type']}")
            col2.caption(f"📅 {dates}")
            if days_left is not None and days_left >= 0:
                col3.caption(f"⏳ {days_left} day(s) remaining")
            elif days_left is not None:
                col3.caption("🔴 Expired")

        if active_now:
            st.markdown(f"#### ✅ Active Now ({len(active_now)})")
            for r in active_now:
                _render_offer_row(r, "🟢")
            st.markdown("---")

        if upcoming:
            st.markdown(f"#### 🔜 Upcoming ({len(upcoming)})")
            for r in upcoming:
                days_to_start = (r["_vf"] - today).days
                _render_offer_row(r, f"🕐 Starts in {days_to_start}d")
            st.markdown("---")

        if expired:
            with st.expander(f"🗂️ Expired Offers ({len(expired)})"):
                for r in expired:
                    _render_offer_row(r, "⚫")

    # ── Quick create timed offer ──────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### ➕ Quick Create Timed Offer")
    with st.form("quick_timed_offer_form", clear_on_submit=True):
        q1, q2 = st.columns(2)
        offer_name  = q1.text_input("Offer Name *", placeholder="Diwali 2026 — 15% off")
        offer_type  = q2.selectbox("Type", ["party","product","brand_group","coating","offer_slab","offer_bogo","promo_code"])

        v1, v2, v3, v4, v5 = st.columns(5)
        offer_value = v1.number_input("Discount %", value=10.0, min_value=0.0, step=1.0)
        valid_from  = v2.date_input("Valid From *")
        valid_to    = v3.date_input("Valid To *", value=today + timedelta(days=7))
        time_from   = v4.text_input("Start Time", placeholder="18:00", help="Optional HH:MM — blank = all day")
        time_to     = v5.text_input("End Time",   placeholder="22:00", help="Optional HH:MM")

        active_days = st.multiselect(
            "Active Days (blank = every day)",
            ["MON","TUE","WED","THU","FRI","SAT","SUN"],
            help="SAT + SUN = weekend only. Blank = all days."
        )

        t1, t2 = st.columns(2)
        party_tags  = t1.text_input("Party Tags", placeholder="wholesale, vip")
        channel     = t2.selectbox("Channel", ["all","wholesale","retail","online"])

        if st.form_submit_button("💾 Save Timed Offer", type="primary"):
            if not offer_name:
                st.error("Offer name required.")
            elif valid_to < valid_from:
                st.error("Valid To must be after Valid From.")
            else:
                ptags = [p.strip() for p in party_tags.split(",") if p.strip()]
                cond  = {
                    "channel":    channel,
                    "valid_from": valid_from.isoformat(),
                    "valid_to":   valid_to.isoformat(),
                }
                if ptags:
                    cond["party_tags"] = ptags
                if active_days:
                    cond["days"] = active_days
                if time_from.strip() and time_to.strip():
                    cond["time_from"] = time_from.strip()
                    cond["time_to"]   = time_to.strip()
                rule_data = {
                    "name":             offer_name,
                    "description":      f"Timed offer: {valid_from} → {valid_to}",
                    "type":             offer_type,
                    "value_type":       "percent",
                    "value":            offer_value,
                    "gst_rate":         12,
                    "priority":         4,
                    "stackable":        True,
                    "display_label":    offer_name,
                    "icon_emoji":       "⏰",
                    "show_in_offers":   True,
                    "conflict_strategy": "best_price",
                    "conditions":       cond,
                }
                if _save_rule_to_db(rule_data):
                    try:
                        from modules.pricing.discount_engine import invalidate_rule_cache
                        invalidate_rule_cache()
                    except Exception:
                        pass
                    st.success(
                        f"✅ **{offer_name}** saved. "
                        f"Activates {valid_from.strftime('%d %b')} → {valid_to.strftime('%d %b %Y')} automatically."
                    )
                    st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3B — PRICING TIERS
# ════════════════════════════════════════════════════════════════════════════
def _tab_pricing_tiers():
    """
    Phase 3B — Customer Pricing Tiers.
    Adds price_tier column to parties table. The engine reads it via
    _get_party_type() → _map_party_type_to_tags() which we extend to
    include tier tags (vip, gold, silver, standard).
    Tier rules fire via party_tags = ["vip"] etc. — no separate rule type needed.
    """
    st.markdown("### 🎯 Pricing Tiers")
    st.caption(
        "Assign a pricing tier to each party. Tier tags flow into the discount engine "
        "automatically — create rules with party_tags matching the tier name."
    )

    # ── Ensure price_tier column exists ──────────────────────────────────────
    try:
        from modules.sql_adapter import run_write, run_query
        run_write("""
            ALTER TABLE parties
            ADD COLUMN IF NOT EXISTS price_tier TEXT DEFAULT 'standard'
        """)
        # Ensure pricing_tiers master table exists
        run_write("""
            CREATE TABLE IF NOT EXISTS pricing_tiers (
                id              SERIAL PRIMARY KEY,
                tier_name       TEXT UNIQUE NOT NULL,
                discount_percent FLOAT NOT NULL DEFAULT 0,
                allow_stacking  BOOLEAN NOT NULL DEFAULT TRUE,
                description     TEXT DEFAULT ''
            )
        """)
        # Seed defaults if empty
        run_write("""
            INSERT INTO pricing_tiers (tier_name, discount_percent, allow_stacking, description)
            VALUES
                ('vip',      20, TRUE,  'VIP customers — best discount, stackable'),
                ('gold',     15, TRUE,  'Gold tier'),
                ('silver',   10, TRUE,  'Silver tier'),
                ('standard',  0, TRUE,  'Default — no tier discount'),
                ('doctor',   10, TRUE,  'Doctors — wholesale channel'),
                ('dealer',    8, TRUE,  'Dealers')
            ON CONFLICT (tier_name) DO NOTHING
        """)
    except Exception as _e:
        st.warning(f"Could not ensure pricing tiers: {_e}")

    # ── Pricing Tiers Master Table ────────────────────────────────────────────
    st.markdown("#### 🎯 Tier Discount Master")
    st.caption(
        "Define the base discount % for each tier. "
        "Engine takes MAX(tier_discount, best_rule_discount) as the base, "
        "then stacks slab/promo on top."
    )
    try:
        from modules.sql_adapter import run_query, run_write
        tiers_master = run_query("""
            SELECT id, tier_name, discount_percent, allow_stacking,
                   COALESCE(description,'') AS description
            FROM pricing_tiers
            ORDER BY discount_percent DESC
        """) or []
    except Exception:
        tiers_master = []

    if tiers_master:
        for tm in tiers_master:
            tc1, tc2, tc3, tc4 = st.columns([2, 2, 2, 1])
            new_pct = tc1.number_input(
                f"{tm['tier_name'].upper()} discount %",
                value=float(tm.get("discount_percent") or 0),
                min_value=0.0, max_value=100.0, step=1.0,
                key=f"tier_pct_{tm['id']}"
            )
            new_stack = tc2.checkbox(
                "Stackable",
                value=bool(tm.get("allow_stacking", True)),
                key=f"tier_stack_{tm['id']}",
                help="Untick to block slab/promo stacking for this tier"
            )
            tc3.caption(str(tm.get("description") or ""))
            if tc4.button("💾", key=f"save_tier_master_{tm['id']}",
                          help="Save this tier"):
                try:
                    run_write(
                        "UPDATE pricing_tiers SET discount_percent=%s, "
                        "allow_stacking=%s WHERE id=%s",
                        (new_pct, new_stack, tm["id"])
                    )
                    # Invalidate tier cache
                    try:
                        from modules.pricing.discount_engine import _invalidate_tier_cache
                        _invalidate_tier_cache()
                    except Exception:
                        pass
                    st.success(f"✅ {tm['tier_name'].upper()} → {new_pct:.0f}%")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Save failed: {_e}")

    # Add new tier
    with st.expander("➕ Add Custom Tier"):
        with st.form("add_tier_form", clear_on_submit=True):
            nt1, nt2, nt3 = st.columns(3)
            new_tier_name = nt1.text_input("Tier Name", placeholder="platinum")
            new_tier_pct  = nt2.number_input("Discount %", value=0.0,
                                              min_value=0.0, max_value=100.0, step=1.0)
            new_tier_stack = nt3.checkbox("Stackable", value=True)
            new_tier_desc  = st.text_input("Description", placeholder="Platinum customers")
            if st.form_submit_button("Add Tier"):
                if not new_tier_name.strip():
                    st.error("Tier name required.")
                else:
                    try:
                        run_write(
                            "INSERT INTO pricing_tiers "
                            "(tier_name, discount_percent, allow_stacking, description) "
                            "VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (tier_name) DO UPDATE SET "
                            "discount_percent=EXCLUDED.discount_percent, "
                            "allow_stacking=EXCLUDED.allow_stacking",
                            (new_tier_name.strip().lower(), new_tier_pct,
                             new_tier_stack, new_tier_desc.strip())
                        )
                        st.success(f"✅ Tier '{new_tier_name}' saved.")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Failed: {_e}")

    st.markdown("---")

    # ── Tier summary ─────────────────────────────────────────────────────────
    st.markdown("#### Current Tier Distribution")
    try:
        from modules.sql_adapter import run_query
        tier_dist = run_query("""
            SELECT
                COALESCE(price_tier, 'standard') AS tier,
                COUNT(*) AS parties,
                (SELECT STRING_AGG(pn, ', ') FROM (
                    SELECT party_name AS pn FROM parties p2
                    WHERE COALESCE(p2.price_tier, 'standard') = COALESCE(parties.price_tier, 'standard')
                      AND COALESCE(p2.is_active, TRUE) = TRUE
                    ORDER BY p2.party_name LIMIT 3
                ) sub)                      AS examples
            FROM parties
            WHERE COALESCE(is_active, TRUE) = TRUE
            GROUP BY price_tier
            ORDER BY parties DESC
        """) or []
    except Exception:
        tier_dist = []

    TIER_ICON = {
        "vip":      "⭐ VIP",
        "gold":     "🥇 Gold",
        "silver":   "🥈 Silver",
        "standard": "🔵 Standard",
        "wholesale": "🏭 Wholesale",
    }

    if tier_dist:
        cols = st.columns(min(len(tier_dist), 5))
        for i, t in enumerate(tier_dist):
            tier   = str(t.get("tier") or "standard")
            label  = TIER_ICON.get(tier, f"• {tier.title()}")
            with cols[i % 5]:
                st.metric(label, int(t.get("parties") or 0))
                st.caption(str(t.get("examples") or "")[:60])
    else:
        st.info("No parties found.")

    st.markdown("---")

    # ── Assign tier to party ──────────────────────────────────────────────────
    st.markdown("#### Assign Tier to Party")
    try:
        from modules.sql_adapter import run_query, run_write
        parties = run_query("""
            SELECT id::text AS id, party_name,
                   COALESCE(price_tier, 'standard') AS price_tier,
                   COALESCE(party_type, '') AS party_type
            FROM parties
            WHERE COALESCE(is_active, TRUE) = TRUE
            ORDER BY party_name
        """) or []
    except Exception:
        parties = []

    if not parties:
        st.info("No parties found in DB.")
    else:
        TIER_OPTIONS = ["standard", "silver", "gold", "vip", "wholesale", "retail", "dealer"]
        search = st.text_input("Search party name", placeholder="Type to filter...")
        filtered = [p for p in parties
                    if not search or search.lower() in str(p.get("party_name","")).lower()]

        for p in filtered[:50]:
            c1, c2, c3 = st.columns([4, 2, 1])
            c1.markdown(f"**{p['party_name']}** `{p.get('party_type','')}`")
            new_tier = c2.selectbox(
                "Tier",
                TIER_OPTIONS,
                index=TIER_OPTIONS.index(p["price_tier"])
                      if p["price_tier"] in TIER_OPTIONS else 0,
                key=f"tier_{p['id']}",
                label_visibility="collapsed"
            )
            if c3.button("✅", key=f"save_tier_{p['id']}", help="Save tier"):
                try:
                    run_write(
                        "UPDATE parties SET price_tier=%s WHERE id=%s::uuid",
                        (new_tier, p["id"])
                    )
                    # Invalidate party type cache in engine
                    try:
                        from modules.pricing.discount_engine import _PARTY_TYPE_CACHE
                        _PARTY_TYPE_CACHE.clear()
                    except Exception:
                        pass
                    st.success(f"✅ {p['party_name']} → {new_tier}")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Save failed: {_e}")

    st.markdown("---")

    # ── How to use tiers ──────────────────────────────────────────────────────
    with st.expander("📖 How Pricing Tiers Work"):
        st.markdown("""
**Step 1 — Assign tier here**
Set each party's tier: Standard / Silver / Gold / VIP / Wholesale / Dealer

**Step 2 — Create tier-based rule in Add Rule tab**
Set *Party Tags* to the tier name, e.g. `vip`
Set discount value, e.g. 20%

**How it fires**
When an order comes in for that party:
- Engine reads `parties.price_tier` → e.g. `"vip"`
- Passes as party tag: `party_tags = ["vip"]`
- Rule with `conditions.party_tags = ["vip"]` matches → 20% discount fires

**Tier tag flow**
```
Party tier = "vip"
  → _map_party_type_to_tags() → ["vip", "wholesale"]
  → Rule party_tags = ["vip"] → matches → fires
```

**Standard tags automatically set**
- doctor / distributor / dealer → also gets ["wholesale"] tag
- retailer / customer → also gets ["retail"] tag
- Any tier → also added as its own tag
        """)


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3D — AI PRICE SUGGESTIONS
# ════════════════════════════════════════════════════════════════════════════
def _tab_ai_suggestions():
    """
    Phase 3D — AI Price Suggestions (10/10 Production Grade).
    Multi-factor: margin × velocity × discount ratio.
    Delegates to modules/analytics/price_suggestions.py.
    """
    try:
        from modules.analytics.price_suggestions import render_price_suggestions
        render_price_suggestions()
        return
    except ImportError:
        pass
    try:
        import os, importlib.util
        _pp = os.path.join(os.path.dirname(__file__), "price_suggestions.py")
        if os.path.exists(_pp):
            spec = importlib.util.spec_from_file_location("ps", _pp)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.render_price_suggestions()
            return
    except Exception:
        pass
    # Last resort: original inline version
    """
    Phase 3D — AI Price Suggestions.
    Analyses margin + discount data to surface actionable recommendations.
    IMPORTANT: Suggestions are read-only — nothing auto-applies.
    Operator must go to Add Rule / Party Discounts to act on any suggestion.
    """
    st.markdown("### 🤖 AI Price Suggestions")
    st.caption(
        "Suggestions based on margin analysis of recent orders. "
        "**Nothing is applied automatically — all actions are manual.**"
    )

    period = st.selectbox(
        "Analyse period", [30, 60, 90],
        format_func=lambda x: f"Last {x} days", index=1, key="ai_period"
    )

    if st.button("🔍 Run Analysis", type="primary"):
        with st.spinner("Analysing discount and margin data…"):
            try:
                from modules.sql_adapter import run_query
                suggestions = []

                # ── Signal 1: Rules giving too much discount (margin hard-stops) ─
                over_disc = run_query("""
                    SELECT
                        COALESCE(ol.discount_rule, 'Unknown') AS rule_name,
                        COUNT(*) AS occurrences,
                        COALESCE(AVG(ol.discount_percent), 0) AS avg_disc_pct,
                        COALESCE(AVG(ol.margin_pct), 0)       AS avg_margin_pct
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE o.created_at >= NOW() - INTERVAL '%s days'
                      AND ol.margin_status IN ('soft_warning', 'hard_stop')
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                      AND ol.discount_rule IS NOT NULL AND ol.discount_rule != ''
                    GROUP BY ol.discount_rule
                    HAVING COUNT(*) >= 3
                    ORDER BY avg_margin_pct ASC
                    LIMIT 5
                """, (period,)) or []

                for r in over_disc:
                    avg_disc = float(r.get("avg_disc_pct") or 0)
                    avg_mrgn = float(r.get("avg_margin_pct") or 0)
                    safer_disc = max(0.0, round(avg_disc - (5.0 - avg_mrgn) - 1, 1))
                    suggestions.append({
                        "priority": "🔴 High",
                        "signal":   "Margin erosion",
                        "finding":  f"Rule **{r['rule_name']}** fires {r['occurrences']}x "
                                    f"with avg margin {avg_mrgn:.1f}% (below safe threshold).",
                        "action":   f"Reduce discount from {avg_disc:.1f}% → ~{safer_disc:.1f}% "
                                    f"in Admin UI → Active Rules to restore margin.",
                    })

                # ── Signal 2: Products never discounted but high gross value ────
                missed = run_query("""
                    SELECT
                        COALESCE(p.product_name, 'Unknown') AS product,
                        COALESCE(p.main_group, '')           AS category,
                        COUNT(ol.id)                         AS lines,
                        COALESCE(SUM(ol.unit_price * ol.quantity), 0) AS gross_value
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    LEFT JOIN products p ON p.id = ol.product_id
                    WHERE o.created_at >= NOW() - INTERVAL '%s days'
                      AND COALESCE(ol.discount_amount, 0) = 0
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                      AND o.order_type = 'WHOLESALE'
                    GROUP BY p.product_name, p.main_group
                    HAVING SUM(ol.unit_price * ol.quantity) > 5000
                       AND COUNT(ol.id) >= 5
                    ORDER BY gross_value DESC
                    LIMIT 3
                """, (period,)) or []

                for r in missed:
                    suggestions.append({
                        "priority": "🟡 Medium",
                        "signal":   "Untapped discount opportunity",
                        "finding":  f"Product **{r['product']}** ({r['category']}) "
                                    f"has ₹{float(r['gross_value']):,.0f} gross value "
                                    f"over {r['lines']} lines with no discount applied.",
                        "action":   "Consider a product-level discount rule to improve "
                                    "competitiveness for wholesale parties.",
                    })

                # ── Signal 3: Parties with no discount receiving above-avg orders ─
                no_disc_parties = run_query("""
                    SELECT
                        o.party_name,
                        COUNT(DISTINCT o.id)                          AS orders,
                        COALESCE(SUM(ol.unit_price * ol.quantity), 0) AS gross_value
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE o.created_at >= NOW() - INTERVAL '%s days'
                      AND COALESCE(ol.discount_amount, 0) = 0
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                      AND o.order_type = 'WHOLESALE'
                    GROUP BY o.party_name
                    HAVING COUNT(DISTINCT o.id) >= 5
                       AND SUM(ol.unit_price * ol.quantity) > 10000
                    ORDER BY gross_value DESC
                    LIMIT 3
                """, (period,)) or []

                for r in no_disc_parties:
                    suggestions.append({
                        "priority": "🟡 Medium",
                        "signal":   "High-value party with no discount",
                        "finding":  f"Party **{r['party_name']}** placed {r['orders']} orders "
                                    f"totalling ₹{float(r['gross_value']):,.0f} with 0 discount.",
                        "action":   "Create a party-level rule in Party Discounts tab "
                                    "to reward loyalty and encourage repeat orders.",
                    })

                # ── Signal 4: Rules with very high discount (>25%) ───────────────
                high_disc = run_query("""
                    SELECT
                        COALESCE(ol.discount_rule, 'Unknown') AS rule_name,
                        COUNT(*) AS lines,
                        COALESCE(AVG(ol.discount_percent), 0) AS avg_disc_pct,
                        COALESCE(SUM(ol.discount_amount), 0)  AS total_given
                    FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE o.created_at >= NOW() - INTERVAL '%s days'
                      AND COALESCE(ol.discount_percent, 0) > 25
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                    GROUP BY ol.discount_rule
                    HAVING COUNT(*) >= 2
                    ORDER BY total_given DESC
                    LIMIT 3
                """, (period,)) or []

                for r in high_disc:
                    suggestions.append({
                        "priority": "🟠 Review",
                        "signal":   "Aggressive discount (>25%)",
                        "finding":  f"Rule **{r['rule_name']}** averages "
                                    f"{float(r['avg_disc_pct']):.1f}% discount across "
                                    f"{r['lines']} lines (₹{float(r['total_given']):,.0f} total).",
                        "action":   "Review if this level of discount is intentional. "
                                    "Check margin status for these lines in Analytics tab.",
                    })

                # ── Render suggestions ────────────────────────────────────────
                if not suggestions:
                    st.success("✅ No issues found. Margins look healthy for this period.")
                else:
                    st.markdown(f"**{len(suggestions)} suggestion(s) found:**")
                    st.markdown("---")
                    for i, s in enumerate(suggestions, 1):
                        with st.container():
                            c1, c2 = st.columns([1, 5])
                            c1.markdown(f"**{s['priority']}**")
                            c2.markdown(
                                f"**{s['signal']}**  \n"
                                f"{s['finding']}  \n"
                                f"💡 **Suggested action:** {s['action']}"
                            )
                            st.markdown("---")

                    st.info(
                        "⚠️ All suggestions above are informational only. "
                        "No changes have been made to any rules. "
                        "Use **Active Rules**, **Party Discounts**, or **Add Rule** tabs to act."
                    )

            except Exception as e:
                st.error(f"Analysis failed: {e}")
                st.caption("Ensure Phase 2A migration has run and order_lines has discount/margin columns.")

    else:
        st.markdown("""
**What this analyses:**
- 🔴 Rules causing margin erosion (margin < 5% repeatedly)
- 🟡 High-value wholesale products/parties receiving no discount (loyalty gap)
- 🟠 Rules with aggressive discounts (>25%) worth reviewing

**What this never does:**
- Does NOT change any rule automatically
- Does NOT apply any discount
- Does NOT modify any order data

All suggestions require a human to act in the Admin UI.
        """)


# ════════════════════════════════════════════════════════════════════════════
# CLUB OFFERS TAB
# ════════════════════════════════════════════════════════════════════════════
def _tab_club_offers():
    """
    Club Offers — "Buy Product A + Product B together → get discount on B"

    Examples:
      • Buy Platinum Lens (UltraView) → Get SilkLens Cleaner FREE
      • Buy any 3 Contact Lens boxes → Get Lens Case at 50% off
      • Buy Frame + Lens together → Get AR Coating at ₹0

    Club offers fire at CART level — they require both products to be
    in the cart at the same time. This is different from regular discount
    rules which fire per-line.
    """
    st.markdown("### 🤝 Club Offers")
    st.caption(
        "Club offers fire when two products are bought together. "
        "The reward product gets a discount automatically when the trigger product is in the cart."
    )

    # ── Ensure club_offers table exists ──────────────────────────────────────
    try:
        from modules.sql_adapter import run_write
        run_write("""
            CREATE TABLE IF NOT EXISTS club_offers (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name                TEXT NOT NULL,
                description         TEXT,
                trigger_product_ids JSONB NOT NULL DEFAULT '[]',
                trigger_brand       TEXT,
                trigger_main_group  TEXT,
                reward_product_ids  JSONB NOT NULL DEFAULT '[]',
                reward_main_group   TEXT,
                value_type          TEXT NOT NULL DEFAULT 'percent',
                value               NUMERIC(10,4),
                min_trigger_qty     INT  NOT NULL DEFAULT 1,
                channel             TEXT NOT NULL DEFAULT 'all',
                stackable           BOOLEAN NOT NULL DEFAULT TRUE,
                priority            INT  NOT NULL DEFAULT 3,
                valid_from          DATE,
                valid_to            DATE,
                display_label       TEXT DEFAULT '',
                icon_emoji          TEXT DEFAULT '🤝',
                active              BOOLEAN NOT NULL DEFAULT TRUE,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    except Exception as _e:
        st.warning(f"Could not ensure club_offers table: {_e}")

    # ── Active club offers ────────────────────────────────────────────────────
    st.markdown("#### Active Club Offers")
    try:
        from modules.sql_adapter import run_query
        club_rules = run_query("""
            SELECT
                id::text, name, description, value_type, value,
                trigger_product_ids, trigger_brand, trigger_main_group,
                reward_product_ids, reward_main_group,
                min_trigger_qty, channel, stackable,
                display_label, icon_emoji,
                valid_from::text, valid_to::text
            FROM club_offers
            WHERE COALESCE(active, TRUE) = TRUE
            ORDER BY priority, name
        """) or []
    except Exception as _e:
        club_rules = []
        st.warning(f"Could not load club offers: {_e}")

    if not club_rules:
        st.info("No club offers yet. Create one below.")
    else:
        for cr in club_rules:
            import json as _j
            t_ids = cr.get("trigger_product_ids") or []
            if isinstance(t_ids, str):
                try: t_ids = _j.loads(t_ids)
                except: t_ids = []
            r_ids = cr.get("reward_product_ids") or []
            if isinstance(r_ids, str):
                try: r_ids = _j.loads(r_ids)
                except: r_ids = []

            vtype = cr.get("value_type", "percent")
            val   = cr.get("value")
            if vtype == "free":
                disc_lbl = "FREE (100%)"
            elif vtype == "percent" and val:
                disc_lbl = f"{float(val):.0f}% off"
            elif vtype == "fixed" and val:
                disc_lbl = f"₹{float(val):.0f} off"
            else:
                disc_lbl = vtype

            trigger_desc = (
                f"{len(t_ids)} product(s)" if t_ids else
                cr.get("trigger_brand") or cr.get("trigger_main_group") or "?"
            )
            reward_desc = (
                f"{len(r_ids)} product(s)" if r_ids else
                cr.get("reward_main_group") or "?"
            )

            c1, c2, c3, c4 = st.columns([3, 3, 2, 1])
            c1.markdown(
                f"**{cr.get('icon_emoji','🤝')} {cr['name']}**  \n"
                f"`{disc_lbl}` on reward"
            )
            c2.caption(
                f"Trigger: {trigger_desc}  \n"
                f"Reward: {reward_desc}  \n"
                f"Channel: {cr.get('channel','all')} | "
                f"Stackable: {'Yes' if cr.get('stackable') else 'No'}"
            )
            if cr.get("valid_from") or cr.get("valid_to"):
                c3.caption(
                    f"📅 {cr.get('valid_from','∞') or '∞'} → "
                    f"{cr.get('valid_to','∞') or '∞'}"
                )
            with c4:
                if st.button("🗑️", key=f"del_club_{cr['id']}",
                             help="Deactivate this club offer"):
                    try:
                        from modules.sql_adapter import run_write
                        run_write(
                            "UPDATE club_offers SET active=FALSE, "
                            "updated_at=NOW() WHERE id=%s::uuid",
                            (cr["id"],)
                        )
                        from modules.pricing.club_engine import invalidate_club_cache
                        invalidate_club_cache()
                        st.success("Deactivated.")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Failed: {_e}")
        st.markdown("---")

    # ── Create new club offer ─────────────────────────────────────────────────
    st.markdown("#### Create Club Offer")

    with st.expander("➕ New Club Offer", expanded=True):
        # ── Product search helpers ────────────────────────────────────────────
        try:
            from modules.sql_adapter import run_query as _rq
            all_products = _rq("""
                SELECT id::text AS id, product_name,
                       COALESCE(brand,'') AS brand,
                       COALESCE(main_group,'') AS main_group
                FROM products
                WHERE COALESCE(is_active, TRUE) = TRUE
                ORDER BY brand, product_name
                LIMIT 500
            """) or []
        except Exception:
            all_products = []

        prod_options = {
            p["id"]: f"{p['brand']} — {p['product_name']} ({p['main_group']})"
            for p in all_products
        }

        with st.form("club_offer_form", clear_on_submit=True):
            f1, f2 = st.columns(2)
            offer_name = f1.text_input(
                "Offer Name *",
                placeholder="Buy Platinum Lens → Get SilkLens Cleaner Free"
            )
            offer_icon = f2.text_input("Icon", value="🤝")

            description = st.text_input(
                "Description",
                placeholder="Purchase any UltraView Platinum lens and get SilkLens Cleaner at no charge"
            )

            st.markdown("**🎯 Trigger — What must be in the cart?**")
            st.caption("Match by specific products OR by brand OR by category (use whichever is simpler)")

            t1, t2, t3 = st.columns(3)
            trigger_products = t1.multiselect(
                "Specific Trigger Products",
                options=list(prod_options.keys()),
                format_func=lambda x: prod_options.get(x, x),
                help="Customer must have AT LEAST ONE of these products in cart"
            )
            trigger_brand = t2.text_input(
                "OR Trigger Brand",
                placeholder="UltraView",
                help="Any product of this brand acts as trigger"
            )
            trigger_cat = t3.text_input(
                "OR Trigger Category",
                placeholder="ophthalmic_lens",
                help="Any product in this main_group acts as trigger"
            )
            min_qty = st.number_input(
                "Minimum Trigger Qty", value=1, min_value=1, step=1,
                help="How many trigger units must be in cart"
            )

            st.markdown("**🎁 Reward — What gets the discount?**")
            r1, r2 = st.columns(2)
            reward_products = r1.multiselect(
                "Specific Reward Products",
                options=list(prod_options.keys()),
                format_func=lambda x: prod_options.get(x, x),
                help="These products get the club discount"
            )
            reward_cat = r2.text_input(
                "OR Reward Category",
                placeholder="lens_cleaner",
                help="All products in this category get the discount"
            )

            st.markdown("**💰 Discount**")
            d1, d2, d3 = st.columns(3)
            disc_type = d1.selectbox(
                "Discount Type",
                ["free", "percent", "fixed"],
                format_func=lambda x: {
                    "free":    "FREE (100% off reward)",
                    "percent": "Percent % off reward",
                    "fixed":   "Fixed ₹ off reward",
                }.get(x, x)
            )
            disc_value = d2.number_input(
                "Value (% or ₹, ignored for FREE)",
                value=0.0, min_value=0.0, step=5.0
            )
            stackable = d3.checkbox(
                "Stackable",
                value=True,
                help="Stack on top of existing party/product discount on reward line"
            )

            st.markdown("**📅 Optional: Date Range & Channel**")
            v1, v2, v3, v4 = st.columns(4)
            valid_from = v1.date_input("Valid From", value=None)
            valid_to   = v2.date_input("Valid To",   value=None)
            channel    = v3.selectbox("Channel",
                                      ["all","wholesale","retail","online"])
            display_lbl = v4.text_input(
                "Display Label",
                placeholder="SilkLens Cleaner FREE"
            )

            submitted = st.form_submit_button("💾 Save Club Offer", type="primary")

            if submitted:
                errors = []
                if not offer_name:
                    errors.append("Offer name is required.")
                if not trigger_products and not trigger_brand and not trigger_cat:
                    errors.append("Define at least one trigger: product, brand, or category.")
                if not reward_products and not reward_cat:
                    errors.append("Define at least one reward: product or category.")
                if disc_type != "free" and disc_value <= 0:
                    errors.append("Enter a discount value > 0 (or choose FREE).")
                if valid_from and valid_to and valid_to < valid_from:
                    errors.append("Valid To must be after Valid From.")

                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    import json as _j2
                    try:
                        from modules.sql_adapter import run_write as _rw2
                        _rw2("""
                            INSERT INTO club_offers (
                                name, description,
                                trigger_product_ids, trigger_brand, trigger_main_group,
                                reward_product_ids, reward_main_group,
                                value_type, value, min_trigger_qty,
                                channel, stackable, priority,
                                valid_from, valid_to,
                                display_label, icon_emoji, active
                            ) VALUES (
                                %s, %s,
                                %s::jsonb, %s, %s,
                                %s::jsonb, %s,
                                %s, %s, %s,
                                %s, %s, 3,
                                %s, %s,
                                %s, %s, TRUE
                            )
                        """, (
                            offer_name, description or "",
                            _j2.dumps(trigger_products),
                            trigger_brand.strip() or None,
                            trigger_cat.strip() or None,
                            _j2.dumps(reward_products),
                            reward_cat.strip() or None,
                            disc_type,
                            disc_value if disc_type != "free" else None,
                            int(min_qty),
                            channel, stackable,
                            valid_from.isoformat() if valid_from else None,
                            valid_to.isoformat()   if valid_to   else None,
                            display_lbl.strip() or offer_name,
                            offer_icon.strip() or "🤝",
                        ))
                        try:
                            from modules.pricing.club_engine import invalidate_club_cache
                            invalidate_club_cache()
                        except Exception:
                            pass
                        st.success(
                            f"✅ Club offer **{offer_name}** saved!  \n"
                            f"It will fire automatically when both the trigger and "
                            f"reward products are in the same cart."
                        )
                        st.rerun()
                    except Exception as _e2:
                        st.error(f"Save failed: {_e2}")

    # ── How it works ──────────────────────────────────────────────────────────
    with st.expander("📖 How Club Offers Work"):
        st.markdown("""
**Example: Platinum Lens + SilkLens Cleaner**

| Field | Value |
|---|---|
| Trigger Product | Platinum Lens 1.6 (UltraView) |
| Reward Product | SilkLens Cleaner 100ml |
| Discount | FREE (100% off) |
| Stackable | Yes |

**What happens at cart:**
1. Customer adds Platinum Lens → goes to cart normally
2. Customer adds SilkLens Cleaner → club engine fires
3. SilkLens Cleaner gets 100% discount → billed at ₹0
4. Cart shows: `🤝 Club Offer: SilkLens Cleaner FREE`

**Trigger options (pick the most flexible):**
- Specific product IDs → exact match (most precise)
- Brand name → any product of that brand qualifies
- Category → any product in that main_group qualifies

**Reward options:**
- Specific product IDs → only those products get discounted
- Category → all products in that category get discounted

**Stackable:**
- YES → cleaner gets existing 10% party discount + club FREE on top
- NO  → club offer replaces any existing discount on reward line

**Channel:**
- Set to Wholesale / Retail to restrict the offer to one channel
- Leave ALL to fire on any order type
        """)
