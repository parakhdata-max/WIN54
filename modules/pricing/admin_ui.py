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
                   COALESCE(rule_type, type) AS type,
                   priority, value_type, value, special_price,
                   bogo_buy, bogo_get,
                   slab_config::text AS slab_config,
                   gst_rate,
                   conditions::text AS conditions,
                   active,
                   COALESCE(conflict_strategy, 'best_price') AS conflict_strategy,
                   COALESCE(namespace, 'core') AS namespace,
                   COALESCE(stackable, FALSE) AS stackable,
                   COALESCE(display_label, '') AS display_label,
                   COALESCE(icon_emoji, '') AS icon_emoji,
                   COALESCE(show_in_offers, FALSE) AS show_in_offers
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
                name, description, type, rule_type, value_type, value, special_price,
                bogo_buy, bogo_get, slab_config, gst_rate, conditions,
                priority, conflict_strategy, namespace, stackable,
                active, display_label, icon_emoji, show_in_offers
            ) VALUES (
                %(name)s, %(desc)s, %(type)s, %(type)s, %(vtype)s, %(val)s, %(sp)s,
                %(bb)s, %(bg)s, %(slab)s::jsonb, %(gst)s, %(cond)s::jsonb,
                %(pri)s, %(cs)s, %(ns)s, %(stack)s,
                TRUE, %(dlbl)s, %(icon)s, %(sio)s
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
def _render_sidebar():
    with st.sidebar:
        st.markdown("---")
        st.markdown("## 💲 Pricing Admin")

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
                    },
                }
                if _save_rule_to_db(rule_data):
                    st.success("✅ Rule saved to database.")
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
def render_pricing_admin():
    st.markdown(_CSS, unsafe_allow_html=True)
    _render_sidebar()

    st.title("💲 Pricing & Discount Admin")
    st.caption("Manage discount rules, simulate pricing, test validators.")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📋 Active Rules",
        "🧮 Simulator",
        "🧾 Invoice Builder",
        "🏷️ Offers Panel",
        "➕ Add Rule",
        "✅ Validator Test",
    ])

    rules = _load_rules_from_db()

    with tab1: _tab_active_rules(rules)
    with tab2: _tab_simulator(rules)
    with tab3: _tab_invoice_builder(rules)
    with tab4: _tab_offers_panel(rules)
    with tab5: _tab_add_rule()
    with tab6: _tab_validator_test()
