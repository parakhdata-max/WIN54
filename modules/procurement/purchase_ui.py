"""
Purchase Intelligence Module - Real ERP Integration
=====================================================

Wired to:
  - inventory_stock      → real stock levels
  - products             → product master
  - orders + order_lines → sales velocity (30-day rolling)
  - supplier_orders      → existing PO history
  - parties              → supplier master

Run: streamlit run purchase_module_real.py
"""

import streamlit as st
import pandas as pd
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import sys
import os

# ── Path setup: works whether run standalone OR from app root ──────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import your existing SQL adapter
try:
    from modules.sql_adapter import (
        get_connection,
        run_query,
        run_write,
        execute_query,
        read_product_master,
        read_ophthalmic_stock,
        read_product_batch,
        read_frame_sku,
        read_party_master,
        save_supplier_order,
        fetch_supplier_orders,
    )
    DB_CONNECTED = True
except ImportError:
    DB_CONNECTED = False
    st.warning("⚠️ DB adapter not found — running in demo mode with mock data.")

# =============================================================================
# PAGE CONFIG
# =============================================================================

# st.set_page_config removed — handled by app.py

# =============================================================================
# CUSTOM CSS  (same visual language as your original)
# =============================================================================

st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    .category-card {
        border-radius: 12px; padding: 20px; margin: 10px 0;
        border: 2px solid #e2e8f0; transition: all 0.3s ease;
    }
    .category-card:hover { transform: translateY(-2px); box-shadow: 0 8px 16px rgba(0,0,0,0.1); }

    .urgent      { background: linear-gradient(135deg,#fee2e2,#fecaca); border-color:#dc2626; }
    .high-priority{ background: linear-gradient(135deg,#fef3c7,#fde68a); border-color:#f59e0b; }
    .trending    { background: linear-gradient(135deg,#dbeafe,#bfdbfe); border-color:#3b82f6; }
    .seasonal    { background: linear-gradient(135deg,#ede9fe,#ddd6fe); border-color:#8b5cf6; }

    .alert-urgent  { background:#fef2f2; border-left:4px solid #dc2626; padding:12px; border-radius:6px; margin:8px 0; }
    .alert-warning { background:#fffbeb; border-left:4px solid #f59e0b; padding:12px; border-radius:6px; margin:8px 0; }
    .alert-info    { background:#eff6ff; border-left:4px solid #3b82f6; padding:12px; border-radius:6px; margin:8px 0; }

    .stButton > button { border-radius:8px; font-weight:600; transition:all 0.2s ease; }
    .stButton > button:hover { transform:scale(1.02); }

    .po-badge {
        display:inline-block; padding:2px 10px; border-radius:12px;
        font-size:12px; font-weight:600;
    }
    .po-draft    { background:#fef3c7; color:#92400e; }
    .po-sent     { background:#dbeafe; color:#1e40af; }
    .po-received { background:#d1fae5; color:#065f46; }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# DATA LAYER — real DB queries
# =============================================================================

@st.cache_data(ttl=300)   # 5-minute cache so UI stays snappy
def load_inventory_summary() -> pd.DataFrame:
    """
    Aggregate inventory_stock by product → total quantity per product_id.
    Joins products for name, category, purchase_rate (cost).
    """
    if not DB_CONNECTED:
        return _mock_inventory()

    sql = """
        SELECT
            p.id                            AS product_id,
            p.product_name,
            p.brand,
            p.main_group,
            p.category,
            COALESCE(p.unit, 'PCS')         AS unit,
            COALESCE(p.box_size, 1)         AS box_size,

            -- current stock (sum across all power/eye combinations)
            COALESCE(SUM(i.quantity), 0)    AS current_stock,

            -- last known cost from inventory
            MAX(i.purchase_rate)            AS unit_cost,

            -- reorder levels: hardcoded defaults until migration adds these columns
            20   AS min_stock,
            100  AS max_stock,
            10   AS moq,
            7    AS supplier_lead_days

        FROM products p
        LEFT JOIN inventory_stock i ON i.product_id = p.id
            AND COALESCE(i.is_active, true) = true
        WHERE COALESCE(p.is_active, true) = true
        GROUP BY p.id, p.product_name, p.brand, p.main_group,
                 p.category, p.unit, p.box_size
        ORDER BY p.product_name
    """
    try:
        return execute_query(sql, "inventory_summary")
    except Exception as e:
        st.error(f"Inventory load failed: {e}")
        return _mock_inventory()


@st.cache_data(ttl=300)
def load_sales_velocity(days: int = 30) -> pd.DataFrame:
    """
    Rolling avg daily sales per product_id from order_lines.
    Uses completed/delivered orders only (status not CANCELLED).
    """
    if not DB_CONNECTED:
        return _mock_velocity()

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    sql = f"""
        SELECT
            ol.product_id,
            SUM(ol.quantity)                                    AS total_sold,
            ROUND(SUM(ol.quantity)::numeric / {days}, 4)       AS avg_daily_sales,
            COUNT(DISTINCT o.id)                                AS order_count
        FROM order_lines ol
        INNER JOIN orders o ON ol.order_id = o.id
        WHERE o.created_at >= '{since}'
          AND COALESCE(o.status, '') NOT IN ('CANCELLED', 'DELETED')
          AND ol.product_id IS NOT NULL
        GROUP BY ol.product_id
    """
    try:
        return execute_query(sql, "sales_velocity")
    except Exception as e:
        st.error(f"Sales velocity load failed: {e}")
        return _mock_velocity()


@st.cache_data(ttl=300)
def load_supplier_master() -> pd.DataFrame:
    """Load supplier parties (SUPPLIER type)."""
    if not DB_CONNECTED:
        return pd.DataFrame([
            {"party_id": "S1", "party_name": "OptiCo Suppliers"},
            {"party_id": "S2", "party_name": "LensWorld Pvt Ltd"},
        ])
    try:
        df = read_party_master()
        suppliers = df[df["roletype"].str.upper().isin(["SUPPLIER", "VENDOR"])].copy()
        if suppliers.empty:
            suppliers = df.copy()   # fallback: show all parties
        return suppliers
    except Exception as e:
        return pd.DataFrame(columns=["party_id", "party_name"])


@st.cache_data(ttl=120)
def load_open_purchase_orders() -> List[dict]:
    """Fetch non-closed supplier orders for the PO tab."""
    if not DB_CONNECTED:
        return []
    try:
        orders = fetch_supplier_orders()
        return [o for o in orders if o.get("status") not in ("RECEIVED", "CLOSED", "CANCELLED")]
    except Exception:
        return []


# =============================================================================
# BUSINESS LOGIC
# =============================================================================

def build_purchase_candidates(inv_df: pd.DataFrame, vel_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge inventory + velocity, compute urgency + suggested qty.
    Returns one row per product that needs attention.
    """
    # Merge velocity in
    df = inv_df.merge(vel_df[["product_id", "avg_daily_sales", "total_sold"]],
                      on="product_id", how="left")
    df["avg_daily_sales"] = df["avg_daily_sales"].fillna(0.0)
    df["total_sold"]      = df["total_sold"].fillna(0).astype(int)

    # ── Days of stock left ──────────────────────────────────────────────────
    df["days_left"] = df.apply(
        lambda r: (r["current_stock"] / r["avg_daily_sales"])
                  if r["avg_daily_sales"] > 0 else 999,
        axis=1
    )

    # ── Suggested reorder qty (MOQ-rounded) ────────────────────────────────
    SAFETY_BUFFER_DAYS = 7

    def suggest_qty(row):
        total_days   = row["supplier_lead_days"] + SAFETY_BUFFER_DAYS
        proj_sales   = row["avg_daily_sales"] * total_days
        deficit      = max(0, row["min_stock"] - row["current_stock"])
        needed       = proj_sales + deficit
        moq          = max(row["moq"], 1)
        return int(math.ceil(needed / moq) * moq)

    df["suggested_qty"] = df.apply(suggest_qty, axis=1)
    df["suggested_cost"] = df["suggested_qty"] * df["unit_cost"].fillna(0)

    # ── Urgency classification ──────────────────────────────────────────────
    def classify(row):
        if row["days_left"] < 3:
            return "critical", "⚠️ Out of stock in <3 days"
        elif row["current_stock"] < row["min_stock"]:
            return "high", "📉 Below reorder point"
        elif row["avg_daily_sales"] > 0 and row["days_left"] < 10:
            return "trending", f"📈 Fast-moving ({row['avg_daily_sales']:.1f}/day)"
        elif row["suggested_qty"] > 0 and row["current_stock"] < row["max_stock"] * 0.5:
            return "seasonal", "☀️ Stock below 50% max"
        else:
            return None, None

    df[["urgency", "urgency_reason"]] = df.apply(
        lambda r: pd.Series(classify(r)), axis=1
    )

    # Keep only products that need action
    candidates = df[df["urgency"].notna()].copy()
    candidates = candidates.reset_index(drop=True)

    # Assign numeric id for session state tracking
    candidates["id"] = candidates.index + 1

    return candidates


def calculate_suggested_quantity(product: dict) -> int:
    """Single-product wrapper (used in display loops)."""
    SAFETY = 7
    days = product.get("supplier_lead_days", 7) + SAFETY
    proj = product.get("avg_daily_sales", 0) * days
    deficit = max(0, product.get("min_stock", 20) - product.get("current_stock", 0))
    needed = proj + deficit
    moq = max(product.get("moq", 10), 1)
    return int(math.ceil(needed / moq) * moq)


def create_purchase_order(selected_products: List[dict],
                          supplier_id: str,
                          supplier_name: str,
                          notes: str = "") -> bool:
    """
    Build and persist a supplier_order using your existing save_supplier_order().
    """
    if not DB_CONNECTED:
        return True   # demo mode

    now = datetime.now()
    po_id = f"PO-{now.strftime('%Y%m%d%H%M%S')}"

    items = []
    total_qty   = 0
    total_value = 0.0

    for idx, p in enumerate(selected_products, start=1):
        qty  = calculate_suggested_quantity(p)
        cost = float(p.get("unit_cost") or 0)
        items.append({
            "item_no":      idx,
            "product_id":   str(p.get("product_id", "")),
            "product_name": p.get("product_name", "Unknown"),
            "brand":        p.get("brand", ""),
            "eye_side":     None,
            "sph": None, "cyl": None, "axis": None, "add_power": None,
            "ordered_qty":  qty,
            "received_qty": 0,
            "pending_qty":  qty,
            "unit_price":   cost,
            "total_price":  qty * cost,
            "customer_line_id": None,
            "item_status":  "PENDING",
        })
        total_qty   += qty
        total_value += qty * cost

    supplier_order = {
        "supplier_order_id":       po_id,
        "supplier_id":             supplier_id,
        "supplier_name":           supplier_name,
        "customer_order_id":       None,
        "order_date":              now,
        "expected_delivery_date":  now + timedelta(days=14),
        "priority":                "NORMAL",
        "payment_terms":           "NET30",
        "special_instructions":    notes,
        "status":                  "DRAFT",
        "total_items":             len(items),
        "total_qty":               total_qty,
        "total_value":             total_value,
        "created_by":              "purchase_module",
        "created_at":              now,
        "updated_at":              now,
        "items":                   items,
        "status_history": [{
            "status":     "DRAFT",
            "timestamp":  now,
            "notes":      "Created via Smart Purchase Module",
            "changed_by": "purchase_module",
        }],
    }

    try:
        save_supplier_order(supplier_order)
        return True
    except Exception as e:
        st.error(f"PO save failed: {e}")
        return False


# =============================================================================
# MOCK DATA (demo / no-DB mode)
# =============================================================================

def _mock_inventory() -> pd.DataFrame:
    return pd.DataFrame([
        dict(product_id="P1", product_name="Spherical CL -0.50 LE",  brand="AquaLens", main_group="Lenses", category="contact_lenses", unit="BOX", box_size=6, current_stock=5,  unit_cost=125, min_stock=50, max_stock=200, moq=25, supplier_lead_days=7),
        dict(product_id="P2", product_name="Toric Lens -2.75 CYL 180",brand="ToricPro",  main_group="Lenses", category="contact_lenses", unit="BOX", box_size=6, current_stock=3,  unit_cost=215, min_stock=40, max_stock=150, moq=20, supplier_lead_days=10),
        dict(product_id="P3", product_name="Multifocal +1.50 ADD",    brand="FocusPro",  main_group="Lenses", category="contact_lenses", unit="BOX", box_size=6, current_stock=8,  unit_cost=285, min_stock=45, max_stock=180, moq=25, supplier_lead_days=7),
        dict(product_id="P4", product_name="Blue Light Blocking",      brand="ShieldX",   main_group="Eyewear",category="eyewear",        unit="PCS", box_size=1, current_stock=12, unit_cost=450, min_stock=30, max_stock=100, moq=10, supplier_lead_days=5),
        dict(product_id="P5", product_name="Reading Glasses +2.00",    brand="ReadEasy",  main_group="Eyewear",category="eyewear",        unit="PCS", box_size=1, current_stock=18, unit_cost=320, min_stock=35, max_stock=120, moq=12, supplier_lead_days=5),
        dict(product_id="P6", product_name="Lens Cleaning Solution",   brand="CleanClear", main_group="Acc",  category="accessories",     unit="BOX", box_size=6, current_stock=25, unit_cost=85,  min_stock=50, max_stock=200, moq=24, supplier_lead_days=3),
        dict(product_id="P7", product_name="Polarized Sunglasses",     brand="SunShade",  main_group="Eyewear",category="sunglasses",     unit="PCS", box_size=1, current_stock=35, unit_cost=850, min_stock=30, max_stock=150, moq=10, supplier_lead_days=7),
        dict(product_id="P8", product_name="Sports Eyewear Wrap",      brand="ActiveVue", main_group="Eyewear",category="sports",         unit="PCS", box_size=1, current_stock=20, unit_cost=1250,min_stock=20, max_stock=80,  moq=6,  supplier_lead_days=10),
    ])

def _mock_velocity() -> pd.DataFrame:
    return pd.DataFrame([
        dict(product_id="P1", avg_daily_sales=1.2, total_sold=36),
        dict(product_id="P2", avg_daily_sales=0.8, total_sold=24),
        dict(product_id="P3", avg_daily_sales=1.0, total_sold=30),
        dict(product_id="P4", avg_daily_sales=2.5, total_sold=75),
        dict(product_id="P5", avg_daily_sales=1.8, total_sold=54),
        dict(product_id="P6", avg_daily_sales=3.2, total_sold=96),
        dict(product_id="P7", avg_daily_sales=4.5, total_sold=135),
        dict(product_id="P8", avg_daily_sales=2.8, total_sold=84),
    ])


# =============================================================================
# SESSION STATE
# =============================================================================

def _init_state():
    if "pm_candidates"       not in st.session_state: st.session_state.pm_candidates       = None
    if "pm_selected"         not in st.session_state: st.session_state.pm_selected          = set()
    if "pm_supplier_id"      not in st.session_state: st.session_state.pm_supplier_id       = None
    if "pm_supplier_name"    not in st.session_state: st.session_state.pm_supplier_name     = ""
    if "pm_po_notes"         not in st.session_state: st.session_state.pm_po_notes          = ""
    if "pm_last_po"          not in st.session_state: st.session_state.pm_last_po           = None
    if "pm_velocity_days"    not in st.session_state: st.session_state.pm_velocity_days     = 30

# moved into render_purchase_ui: _init_state()


# =============================================================================
# LOAD + BUILD CANDIDATES
# =============================================================================

def _load_candidates(force: bool = False):
    if st.session_state.pm_candidates is None or force:
        with st.spinner("Loading real inventory + sales data..."):
            inv_df = load_inventory_summary()
            vel_df = load_sales_velocity(st.session_state.pm_velocity_days)
            candidates = build_purchase_candidates(inv_df, vel_df)
            st.session_state.pm_candidates = candidates
            # Auto-select critical items
            critical_ids = set(
                candidates.loc[candidates["urgency"] == "critical", "id"].tolist()
            )
            st.session_state.pm_selected = critical_ids

# moved into render_purchase_ui: _load_candidates()
# moved into render_purchase_ui: candidates: pd.DataFrame = st.session_state.pm_candidates


# =============================================================================
# SIDEBAR
# =============================================================================

# OLD TOP-LEVEL SIDEBAR REMOVED — now inside render_purchase_ui()
# =============================================================================
# MAIN — TABS
# =============================================================================


def _log_po_action(po_id: str, action: str, by: str, notes: str = "") -> None:
    """Write to po_approval_log — non-fatal if table doesn't exist yet."""
    try:
        from modules.sql_adapter import run_write as _rw
        _rw("""
            INSERT INTO po_approval_log (po_id, action, actioned_by, notes)
            VALUES (%s::integer, %s, %s, %s)
        """, (po_id, action, by, notes))
    except Exception:
        pass


def render_purchase_ui():
    """Main entry point called by app.py router."""
    _init_state()
    _load_candidates()
    candidates = st.session_state.pm_candidates

    # ── Sidebar purchase controls (appended to app sidebar) ──
    with st.sidebar:
        st.markdown("---")
        st.markdown("## 📊 Purchase")
        if DB_CONNECTED:
            st.success("🟢 DB connected")
        else:
            st.warning("🟡 Demo mode")
        vel_days = st.selectbox(
            "Velocity window",
            [7, 14, 30, 60, 90],
            index=[7,14,30,60,90].index(st.session_state.pm_velocity_days),
            key="pm_vel_days_select"
        )
        if vel_days != st.session_state.pm_velocity_days:
            st.session_state.pm_velocity_days = vel_days
            load_inventory_summary.clear()
            load_sales_velocity.clear()
            _load_candidates(force=True)
            st.rerun()
        if st.button("🔄 Refresh", use_container_width=True, key="pm_refresh_btn"):
            load_inventory_summary.clear()
            load_sales_velocity.clear()
            _load_candidates(force=True)
            st.rerun()
        if candidates is not None and not candidates.empty:
            st.markdown("---")
            n_crit  = len(candidates[candidates.urgency == "critical"])
            n_high  = len(candidates[candidates.urgency == "high"])
            n_trend = len(candidates[candidates.urgency == "trending"])
            n_seas  = len(candidates[candidates.urgency == "seasonal"])
            if n_crit:  st.error(f"🔴 Critical: {n_crit}")
            if n_high:  st.warning(f"🟡 High: {n_high}")
            if n_trend: st.info(f"📈 Trending: {n_trend}")
            if n_seas:  st.info(f"💡 Seasonal: {n_seas}")

    # ── Main content ──
    _render_main_content(candidates)



def _render_main_content(candidates):
    """All the tabs and cards — called from render_purchase_ui."""
    if candidates is None or candidates.empty:
        st.success("✅ All products are adequately stocked.")
        return

    st.title("🛒 Smart Purchase Module")
    st.markdown("### Inventory-Driven Purchase Orders — Powered by Real Data")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs([
        "🎯 Smart Conversion",
        "📋 Purchase Orders",
        "📊 Analytics",
        "🔄 Reorder Monitor",
        "🔀 Supplier Override",
        "⏰ Supplier Schedule",
        "📐 Stock Minimums",
        "🔍 Purchase Register",
        "📤 PO Management",
    ])


    # ─────────────────────────────────────────────────────────────────────────────
    # TAB 1 — SMART CONVERSION
    # ─────────────────────────────────────────────────────────────────────────────

    with tab1:

        if candidates.empty:
            st.success("✅ All products are adequately stocked. No action needed.")
            st.stop()

        st.markdown("---")

        col_left, col_right = st.columns([3, 1])
        with col_left:
            total_flagged = len(candidates)
            st.markdown(f"### Engine analysed inventory — **{total_flagged} products** need attention")
        with col_right:
            view = st.selectbox("View", ["Categories", "Individual Items"], label_visibility="collapsed")

        st.markdown("")

        # ── Group by urgency ──────────────────────────────────────────────────
        groups = {
            "critical":  candidates[candidates.urgency == "critical"],
            "high":      candidates[candidates.urgency == "high"],
            "trending":  candidates[candidates.urgency == "trending"],
            "seasonal":  candidates[candidates.urgency == "seasonal"],
        }

        group_meta = {
            "critical": ("🔴 Critical",      "urgent",       "Out of stock in <3 days"),
            "high":     ("🟡 High Priority", "high-priority","Below reorder point"),
            "trending": ("📈 Trending Up",   "trending",     "Fast-moving items"),
            "seasonal": ("💡 Seasonal",      "seasonal",     "Plan ahead"),
        }

        # ── Category cards ────────────────────────────────────────────────────
        if view == "Categories":
            for urgency_key, (label, css_class, subtitle) in group_meta.items():
                group = groups[urgency_key]
                if group.empty:
                    continue

                ids = set(group["id"].tolist())
                total_cost = sum(
                    calculate_suggested_quantity(r) * float(r.get("unit_cost") or 0)
                    for _, r in group.iterrows()
                )

                with st.container():
                    st.markdown(f'<div class="category-card {css_class}">', unsafe_allow_html=True)
                    col1, col2, col3 = st.columns([2, 2, 1])
                    with col1:
                        st.markdown(f"### {label}")
                        st.markdown(f"**{len(group)} items** — {subtitle}")
                    with col2:
                        st.metric("Estimated Cost", f"₹{total_cost:,.0f}")
                    with col3:
                        all_sel = ids.issubset(st.session_state.pm_selected)
                        if st.checkbox("Select All", value=all_sel, key=f"chk_{urgency_key}"):
                            st.session_state.pm_selected |= ids
                        else:
                            st.session_state.pm_selected -= ids

                    with st.expander(f"View {len(group)} items"):
                        for _, row in group.iterrows():
                            is_sel = row["id"] in st.session_state.pm_selected
                            qty = calculate_suggested_quantity(row.to_dict())
                            col_a, col_b = st.columns([4, 1])
                            with col_a:
                                days_left_str = (
                                    f"{row['days_left']:.0f} days" if row['days_left'] < 999
                                    else "∞"
                                )
                                st.markdown(
                                    f"**{row['product_name']}** ({row.get('brand','')})<br>"
                                    f"Stock: **{int(row['current_stock'])}** | "
                                    f"Daily sales: **{row['avg_daily_sales']:.1f}** | "
                                    f"Days left: **{days_left_str}** | "
                                    f"Suggest: **{qty} units** "
                                    f"(₹{qty * float(row.get('unit_cost') or 0):,.0f})",
                                    unsafe_allow_html=True,
                                )
                            with col_b:
                                checked = st.checkbox(
                                    "Include",
                                    value=is_sel,
                                    key=f"item_{urgency_key}_{row['id']}"
                                )
                                if checked:
                                    st.session_state.pm_selected.add(row["id"])
                                else:
                                    st.session_state.pm_selected.discard(row["id"])

                    st.markdown("</div>", unsafe_allow_html=True)
                st.markdown("")

        # ── Individual item table ─────────────────────────────────────────────
        else:
            for _, row in candidates.iterrows():
                is_sel = row["id"] in st.session_state.pm_selected
                qty = calculate_suggested_quantity(row.to_dict())
                cost = qty * float(row.get("unit_cost") or 0)
                urgency_emoji = {"critical": "🔴", "high": "🟡", "trending": "📈", "seasonal": "💡"}.get(row["urgency"], "")
                col1, col2, col3, col4 = st.columns([3, 1, 2, 1])
                col1.markdown(f"{urgency_emoji} **{row['product_name']}**  \n{row.get('brand', '')} · {row.get('category', '')}")
                col2.metric("Stock", int(row["current_stock"]))
                col3.metric("Suggest", f"{qty} units · ₹{cost:,.0f}")
                sel = col4.checkbox("", value=is_sel, key=f"ind_{row['id']}")
                if sel:
                    st.session_state.pm_selected.add(row["id"])
                else:
                    st.session_state.pm_selected.discard(row["id"])
                st.divider()

        # ── Totals + PO creation ──────────────────────────────────────────────
        st.markdown("---")
        selected_rows = candidates[candidates["id"].isin(st.session_state.pm_selected)]
        total_items = len(selected_rows)
        total_cost = sum(
            calculate_suggested_quantity(r) * float(r.get("unit_cost") or 0)
            for _, r in selected_rows.iterrows()
        )

        col1, col2, col3 = st.columns(3)
        col1.metric("Selected Items", total_items)
        col2.metric("Total Estimated Cost", f"₹{total_cost:,.0f}")

        st.markdown("")

        # Supplier selection + notes (only show when items are selected)
        if total_items > 0:
            with st.expander("📦 Set Supplier & Notes before creating PO", expanded=True):
                suppliers_df = load_supplier_master()
                supplier_names = ["— Select Supplier —"] + suppliers_df["party_name"].tolist()
                chosen = st.selectbox("Supplier", supplier_names, key="supplier_select")

                if chosen != "— Select Supplier —":
                    row_s = suppliers_df[suppliers_df["party_name"] == chosen].iloc[0]
                    st.session_state.pm_supplier_id   = str(row_s["party_id"])
                    st.session_state.pm_supplier_name = chosen

                notes = st.text_area("Special instructions / notes", value=st.session_state.pm_po_notes, key="po_notes")
                st.session_state.pm_po_notes = notes

        col_btn1, _, col_btn3 = st.columns([1, 1, 1])

        with col_btn3:
            if st.button("Create Purchase Order →", type="primary", use_container_width=True):
                if total_items == 0:
                    st.warning("⚠️ Please select at least one item.")
                elif not st.session_state.pm_supplier_id and DB_CONNECTED:
                    st.warning("⚠️ Please select a supplier.")
                else:
                    selected_list = [r.to_dict() for _, r in selected_rows.iterrows()]
                    success = create_purchase_order(
                        selected_list,
                        st.session_state.pm_supplier_id or "S0",
                        st.session_state.pm_supplier_name or "Demo Supplier",
                        st.session_state.pm_po_notes,
                    )
                    if success:
                        now = datetime.now()
                        po_id = f"PO-{now.strftime('%Y%m%d%H%M%S')}"
                        st.session_state.pm_last_po = {
                            "po_id": po_id,
                            "items": total_items,
                            "cost":  total_cost,
                            "supplier": st.session_state.pm_supplier_name,
                        }
                        st.success(f"✅ PO **{po_id}** created — {total_items} items · ₹{total_cost:,.0f}")
                        st.balloons()
                        # Clear selection + refresh
                        st.session_state.pm_selected = set()
                        load_inventory_summary.clear()
                        st.rerun()


    # ─────────────────────────────────────────────────────────────────────────────
    # TAB 2 — PURCHASE ORDERS
    # ─────────────────────────────────────────────────────────────────────────────

    with tab2:
        st.markdown("### 📋 Open Purchase Orders")

        if st.button("🔄 Refresh POs"):
            load_open_purchase_orders.clear()

        open_pos = load_open_purchase_orders()

        if not open_pos:
            st.info("No open purchase orders found. Create one from the Smart Conversion tab.")
        else:
            for po in open_pos:
                with st.expander(f"🗂️ {po.get('supplier_order_id')} — {po.get('supplier_name')} — {po.get('status')}"):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Items",       po.get("total_items", 0))
                    col2.metric("Total Qty",   po.get("total_qty", 0))
                    col3.metric("Value",       f"₹{float(po.get('total_value', 0)):,.0f}")

                    st.markdown(f"**Order Date:** {po.get('order_date', '—')}  |  "
                                f"**Expected:** {po.get('expected_delivery_date', '—')}")

                    items = po.get("items", [])
                    if items:
                        items_df = pd.DataFrame(items)[
                            ["item_no", "product_name", "ordered_qty", "received_qty",
                             "pending_qty", "unit_price", "total_price", "item_status"]
                        ]
                        st.dataframe(items_df, use_container_width=True)

                    # ── PO Approval gate ──────────────────────────────────────────
                    _po_id  = str(po.get("id") or po.get("supplier_order_id") or "")
                    _po_st  = str(po.get("status") or "Draft")
                    _po_no  = str(po.get("supplier_order_id") or "")
                    _ba1, _ba2, _ba3 = st.columns(3)

                    if _po_st == "Draft":
                        if _ba1.button("📤 Send to Supplier", key=f"po_send_{_po_id}",
                                       type="primary", use_container_width=True):
                            try:
                                from modules.procurement.po_engine import update_po_status
                                update_po_status(_po_id, "Sent")
                                _log_po_action(_po_id, "SUBMITTED",
                                               st.session_state.get("user_name","system"),
                                               "Sent to supplier")
                                st.success(f"✅ {_po_no} sent")
                                st.rerun()
                            except Exception as _e:
                                st.error(str(_e))

                    elif _po_st in ("Sent", "SENT"):
                        _appr_note = _ba1.text_input("Approval note", key=f"po_note_{_po_id}",
                                                      placeholder="optional", label_visibility="collapsed")
                        if _ba2.button("✅ Approve PO", key=f"po_approve_{_po_id}",
                                       type="primary", use_container_width=True):
                            try:
                                from modules.procurement.po_engine import update_po_status
                                from modules.sql_adapter import run_write as _rw_po
                                update_po_status(_po_id, "Confirmed", notes=_appr_note)
                                _rw_po("""
                                    UPDATE supplier_orders
                                    SET approved_by=%s, approved_at=NOW(), approval_notes=%s
                                    WHERE id=%s::uuid
                                """, (st.session_state.get("user_name","system"), _appr_note, _po_id))
                                _log_po_action(_po_id, "APPROVED",
                                               st.session_state.get("user_name","system"),
                                               _appr_note or "Approved")
                                st.success(f"✅ {_po_no} approved")
                                st.rerun()
                            except Exception as _e:
                                st.error(str(_e))
                        if _ba3.button("❌ Reject", key=f"po_reject_{_po_id}",
                                       use_container_width=True):
                            try:
                                from modules.procurement.po_engine import update_po_status
                                update_po_status(_po_id, "Draft", notes="Rejected — returned to draft")
                                _log_po_action(_po_id, "REJECTED",
                                               st.session_state.get("user_name","system"),
                                               "Rejected by manager")
                                st.warning(f"PO {_po_no} returned to Draft")
                                st.rerun()
                            except Exception as _e:
                                st.error(str(_e))

                    elif _po_st in ("Confirmed", "APPROVED"):
                        st.success(f"✅ Approved by {po.get('approved_by','—')}")


    # ─────────────────────────────────────────────────────────────────────────────
    # TAB 3 — ANALYTICS
    # ─────────────────────────────────────────────────────────────────────────────

    with tab3:
        st.markdown("### 📊 Purchase Intelligence Dashboard")

        if candidates.empty:
            st.info("No data loaded yet.")
        else:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Critical Items",    len(candidates[candidates.urgency == "critical"]))
            col2.metric("High Priority",     len(candidates[candidates.urgency == "high"]))
            col3.metric("Trending",          len(candidates[candidates.urgency == "trending"]))
            col4.metric("Seasonal",          len(candidates[candidates.urgency == "seasonal"]))

            st.markdown("---")
            st.markdown("#### Top 10 — Highest Reorder Cost")

            candidates["est_cost"] = candidates.apply(
                lambda r: calculate_suggested_quantity(r.to_dict()) * float(r.get("unit_cost") or 0),
                axis=1
            )
            top10 = candidates.nlargest(10, "est_cost")[
                ["product_name", "brand", "category", "current_stock",
                 "avg_daily_sales", "days_left", "est_cost", "urgency"]
            ].copy()
            top10["days_left"] = top10["days_left"].apply(lambda x: "∞" if x > 500 else f"{x:.0f}")
            top10["est_cost"]  = top10["est_cost"].apply(lambda x: f"₹{x:,.0f}")
            top10["avg_daily_sales"] = top10["avg_daily_sales"].apply(lambda x: f"{x:.2f}")

            st.dataframe(
                top10.rename(columns={
                    "product_name": "Product", "brand": "Brand",
                    "category": "Category", "current_stock": "Stock",
                    "avg_daily_sales": "Sales/Day", "days_left": "Days Left",
                    "est_cost": "Reorder Cost", "urgency": "Urgency"
                }),
                use_container_width=True,
            )

            st.markdown("#### Stock Coverage by Category")
            cat_summary = candidates.groupby("category").agg(
                products=("id", "count"),
                avg_days_left=("days_left", lambda x: x[x < 500].mean() if (x < 500).any() else 0),
                total_reorder=("est_cost", "sum"),
            ).reset_index()
            cat_summary["total_reorder"] = cat_summary["total_reorder"].apply(lambda x: f"₹{x:,.0f}")
            cat_summary["avg_days_left"] = cat_summary["avg_days_left"].apply(lambda x: f"{x:.0f} days")
            st.dataframe(cat_summary.rename(columns={
                "category": "Category", "products": "Products Flagged",
                "avg_days_left": "Avg Days Left", "total_reorder": "Total Reorder Value"
            }), use_container_width=True)

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 4 — REORDER MONITOR
    # ─────────────────────────────────────────────────────────────────────────
    with tab4:
        _render_reorder_monitor()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 5 — SUPPLIER OVERRIDE
    # ─────────────────────────────────────────────────────────────────────────
    with tab5:
        _render_supplier_override()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 6 — SUPPLIER SCHEDULE
    # ─────────────────────────────────────────────────────────────────────────
    with tab6:
        _render_supplier_schedule()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 7 — STOCK MINIMUMS MANAGER
    # ─────────────────────────────────────────────────────────────────────────
    with tab7:
        _render_stock_minimums()

    # ─────────────────────────────────────────────────────────────────────
    # TAB 8 — PURCHASE REGISTER
    # ─────────────────────────────────────────────────────────────────────
    with tab8:
        try:
            from modules.backoffice.purchase_register import render_purchase_register
            render_purchase_register()
        except Exception as _pre8:
            import traceback
            st.error(f"Purchase Register: {_pre8}")
            st.code(traceback.format_exc())

    # TAB 9 — PO MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────
    with tab9:
        try:
            from modules.procurement.po_management import render_po_management
            render_po_management()
        except Exception as _pre9:
            import traceback
            st.error(f"PO Management: {_pre9}")
            st.code(traceback.format_exc())



# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — REORDER MONITOR
# ─────────────────────────────────────────────────────────────────────────────

def _render_reorder_monitor():
    """
    Reorder Monitor — reads from product_stock_minimum.
    Shows every product+power entry where combined stock across all batches
    is below min_qty. Operator can trigger individual POs or run full auto-check.
    """
    st.markdown("### 🔄 Reorder Monitor")
    st.caption(
        "Stock checked at power level — quantities combined across all batches. "
        "Set minimums in the **📐 Stock Minimums** tab."
    )

    # ── Auto-create tables (safe IF NOT EXISTS) ───────────────────────
    try:
        from modules.sql_adapter import run_write as _rw
        _rw("""
            CREATE TABLE IF NOT EXISTS product_stock_minimum (
                id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id       UUID NOT NULL REFERENCES products(id),
                sph              NUMERIC(6,2),
                cyl              NUMERIC(6,2),
                axis             INTEGER,
                add_power        NUMERIC(6,2),
                eye_side         TEXT DEFAULT 'B',
                min_qty          INTEGER NOT NULL DEFAULT 1,
                reorder_qty      INTEGER NOT NULL DEFAULT 1,
                auto_fulfillment BOOLEAN DEFAULT FALSE,
                reorder_enabled  BOOLEAN DEFAULT FALSE,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                updated_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (product_id, sph, cyl, axis, add_power, eye_side)
            )
        """)
        _rw("""
            CREATE TABLE IF NOT EXISTS reorder_log (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id        UUID NOT NULL REFERENCES products(id),
                supplier_id       UUID REFERENCES parties(id),
                source_order_id   TEXT,
                supplier_order_id INTEGER REFERENCES supplier_orders(id),
                triggered_at      TIMESTAMPTZ DEFAULT NOW(),
                expected_delivery DATE,
                status            TEXT DEFAULT 'OPEN',
                resolved_at       TIMESTAMPTZ,
                notes             TEXT
            )
        """)
        _rw("""
            CREATE TABLE IF NOT EXISTS supplier_product_override (
                id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id           UUID NOT NULL REFERENCES products(id),
                override_supplier_id UUID NOT NULL REFERENCES parties(id),
                reason               TEXT,
                is_active            BOOLEAN DEFAULT TRUE,
                created_by           TEXT,
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                updated_at           TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    except Exception:
        pass

    try:
        from modules.sql_adapter import run_query

        # Power-level stock vs minimum — combined across all batches
        rows = run_query("""
            SELECT
                psm.id::text                            AS psm_id,
                psm.product_id::text                    AS product_id,
                p.product_name,
                COALESCE(p.brand, '')                   AS brand,
                psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
                psm.min_qty,
                psm.reorder_qty,
                psm.reorder_enabled,
                psm.auto_fulfillment,
                COALESCE(p.supplier_tat_days, 1)        AS tat_days,
                COALESCE(SUM(i.quantity), 0)            AS combined_qty,
                COALESCE(sup.party_name, '⚠️ Not set')  AS supplier_name
            FROM product_stock_minimum psm
            JOIN products p ON p.id = psm.product_id
            LEFT JOIN inventory_stock i
                   ON i.product_id = psm.product_id
                  AND COALESCE(i.sph,       0) = COALESCE(psm.sph,       0)
                  AND COALESCE(i.cyl,       0) = COALESCE(psm.cyl,       0)
                  AND COALESCE(i.axis,      0) = COALESCE(psm.axis,      0)
                  AND COALESCE(i.add_power, 0) = COALESCE(psm.add_power, 0)
                  AND COALESCE(i.is_active, TRUE) = TRUE
            LEFT JOIN parties sup ON sup.id = p.preferred_supplier_id
            WHERE COALESCE(p.is_active, TRUE) = TRUE
            GROUP BY
                psm.id, psm.product_id, p.product_name, p.brand,
                psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
                psm.min_qty, psm.reorder_qty, psm.reorder_enabled,
                psm.auto_fulfillment, p.supplier_tat_days, sup.party_name
            ORDER BY (COALESCE(SUM(i.quantity),0) - psm.min_qty) ASC
        """) or []

        # Open reorders
        open_reorder_notes = set()
        try:
            rl = run_query("""
                SELECT product_id::text AS pid, notes
                FROM reorder_log WHERE status = 'OPEN'
            """) or []
            open_reorder_notes = {r["pid"] for r in rl}
        except Exception:
            pass

    except Exception as e:
        st.error(f"Could not load reorder data: {e}")
        return

    if not rows:
        st.info(
            "No stock minimums defined yet. "
            "Go to **📐 Stock Minimums** tab to set minimum quantities per product+power."
        )
        return

    below_min  = [r for r in rows if float(r["combined_qty"]) < float(r["min_qty"])]
    reord_enab = [r for r in rows if r["reorder_enabled"]]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Rules",          len(rows))
    c2.metric("Below Minimum",        len(below_min),
              delta=f"-{len(below_min)}" if below_min else None,
              delta_color="inverse")
    c3.metric("Reorder Enabled",      len(reord_enab))
    c4.metric("Auto-Fulfillment",     sum(1 for r in rows if r["auto_fulfillment"]))

    st.markdown("---")
    col_btn, col_adv, col_info = st.columns([1, 1, 2])
    with col_adv:
        if st.button("🤖 Refresh Advisory", use_container_width=True,
                     key="refresh_advisory",
                     help="Recalculates suggested quantities from sales history"):
            try:
                from modules.procurement.po_engine import run_advisory_update
                res = run_advisory_update()
                st.success(
                    f"✅ Advisory updated for {res['updated']}/{res['total']} rules"
                )
                st.rerun()
            except Exception as e:
                st.error(f"Advisory update failed: {e}")
    with col_btn:
        if st.button("🚀 Run Auto Reorder Check", type="primary",
                     use_container_width=True, key="run_reorder_check"):
            try:
                from modules.procurement.po_engine import check_and_trigger_reorders
                result = check_and_trigger_reorders(created_by="purchase_ui")
                for t in result.get("triggered", []):
                    st.success(
                        f"✅ PO {t['po_number']} → {t['supplier']} | "
                        f"{t['product']} {t.get('power','')} × {t['reorder_qty']} | "
                        f"Expected: {t.get('expected_delivery','—')}"
                        + (" [OVERRIDE]" if t.get("is_override") else "")
                    )
                for s in result.get("skipped", []):
                    st.warning(f"⚠️ {s['product']} {s.get('power','')} — {s['reason']}")
                if not result.get("triggered") and not result.get("skipped"):
                    st.info("✅ All reorder-enabled powers are adequately stocked.")
            except Exception as e:
                st.error(f"Reorder check failed: {e}")
    with col_info:
        st.caption(
            "Checks all reorder-enabled powers. Combined stock across all batches "
            "compared against min_qty. Raises PO only when needed and no open reorder exists."
        )

    st.markdown("---")

    # Only show rows that are below minimum or have reorder enabled
    display_rows = [r for r in rows if
                    float(r["combined_qty"]) < float(r["min_qty"])
                    or r["reorder_enabled"]]

    if not display_rows:
        st.success("✅ All defined minimums are met.")
        return

    for r in display_rows:
        avail   = float(r["combined_qty"])
        minq    = float(r["min_qty"])
        is_low  = avail < minq
        has_open = r["product_id"] in open_reorder_notes

        # Build power label
        pp = []
        if r.get("sph")       is not None: pp.append(f"SPH {float(r['sph']):+.2f}")
        if r.get("cyl")       is not None: pp.append(f"CYL {float(r['cyl']):+.2f}")
        if r.get("axis")      is not None: pp.append(f"AX {int(r['axis'])}")
        if r.get("add_power") is not None: pp.append(f"ADD {float(r['add_power']):+.2f}")
        if r.get("eye_side"):              pp.append(f"Eye:{r['eye_side']}")
        power_str = " | ".join(pp) if pp else "All powers"

        badge = "🔴 BELOW MIN" if is_low and not has_open else                 "🟢 REORDER OPEN" if has_open else "✅ OK"

        with st.expander(
            f"{badge}  {r['product_name']} · {r['brand']}  |  {power_str}  "
            f"|  Stock: {avail:.0f} / Min: {minq:.0f}  |  {r['supplier_name']}",
            expanded=is_low and not has_open,
        ):
            co1, co2, co3, co4, co5 = st.columns(5)
            co1.metric("Combined Stock", f"{avail:.0f}")
            co2.metric("Minimum",        f"{minq:.0f}",
                       delta=f"{minq-avail:.0f} needed" if is_low else "OK",
                       delta_color="inverse" if is_low else "normal")
            co3.metric("Suggested Qty",  r.get("suggested_reorder_qty") or "—")
            co4.metric("Avg/Day",        f"{float(r.get('avg_daily_sales') or 0):.2f}")
            co5.metric("Auto-Bill",      "✅" if r["auto_fulfillment"] else "—")

            # Advisory suggestion banner — show when system suggests different min
            sugg_min = r.get("system_suggested_min")
            if sugg_min and abs(sugg_min - int(minq)) >= 2:
                _diff = sugg_min - int(minq)
                _dir  = "increase" if _diff > 0 else "decrease"
                st.markdown(
                    f"<div style='background:#1a1200;border-left:3px solid #f59e0b;"
                    f"border-radius:0 6px 6px 0;padding:6px 12px;font-size:0.78rem'>"
                    f"💡 <b style='color:#fbbf24'>Advisory suggests {_dir} min_qty "
                    f"to {sugg_min}</b>"
                    f"<span style='color:#78716c'> (currently {int(minq)}, "
                    f"avg {float(r.get('avg_daily_sales') or 0):.2f}/day)</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )

            if is_low and not has_open and r["reorder_enabled"]:
                # ── Advisory calculation ──────────────────────────────
                try:
                    from modules.procurement.po_engine import (
                        calculate_smart_reorder_qty, get_effective_supplier,
                        calculate_expected_delivery, create_po, POItem
                    )
                    import datetime as _dt
                    eff   = get_effective_supplier(r["product_id"])
                    smart = calculate_smart_reorder_qty(
                        product_id  = r["product_id"],
                        sph         = r.get("sph"),
                        cyl         = r.get("cyl"),
                        axis        = r.get("axis"),
                        add_power   = r.get("add_power"),
                        min_qty     = int(r["min_qty"]),
                        supplier_id = eff.get("supplier_id"),
                        tat_days    = int(r["tat_days"] or 1),
                    )
                    bd = smart["breakdown"]

                    # ── Advisory breakdown display ────────────────────
                    phase_badge = {
                        "NONE":   "⚪ No data",
                        "EARLY":  "🟡 Early data",
                        "PHASE1": "🔵 3–12 months",
                        "PHASE2": "🟢 12+ months (seasonal)",
                    }.get(smart["phase"], "")

                    st.markdown(
                        f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                        f"border-radius:8px;padding:10px 14px;margin:6px 0'>"
                        f"<div style='color:#60a5fa;font-weight:700;margin-bottom:6px'>"
                        f"📊 Reorder Advisory  <span style='color:#475569;font-size:0.75rem'>"
                        f"{phase_badge} | {smart['months_of_data']:.0f} months data</span></div>"
                        f"<table style='width:100%;font-size:0.78rem;color:#94a3b8'>"
                        f"<tr><td>Min Qty</td><td style='color:#e2e8f0;text-align:right'><b>{bd['min_qty']}</b></td></tr>"
                        f"<tr><td>Current Stock</td><td style='color:#10b981;text-align:right'>− {bd['current_stock']}</td></tr>"
                        f"<tr><td>Pending Orders (unbilled)</td><td style='color:#f59e0b;text-align:right'>+ {bd['pending_demand']}</td></tr>"
                        f"<tr><td>Stock in Transit (open POs)</td><td style='color:#06b6d4;text-align:right'>− {bd['stock_in_transit']}</td></tr>"
                        f"<tr><td>TAT Buffer ({r['tat_days']}d × {smart['avg_daily_sales']:.2f}/day)</td>"
                        f"<td style='color:#a78bfa;text-align:right'>+ {bd['tat_demand']:.1f}</td></tr>"
                        f"<tr style='border-top:1px solid #1e3a5f'>"
                        f"<td style='color:#e2e8f0;font-weight:700'>Recommended Qty</td>"
                        f"<td style='color:#10b981;font-weight:900;font-size:1rem;text-align:right'>"
                        f"{smart['reorder_qty']}</td></tr>"
                        f"</table>"
                        f"<div style='color:#64748b;font-size:0.7rem;margin-top:6px'>{smart['advisory_note']}</div>"
                        f"</div>",
                        unsafe_allow_html=True
                    )

                    # Operator can override qty
                    final_qty = st.number_input(
                        "Order Qty (edit if needed)",
                        min_value=1,
                        value=smart["reorder_qty"],
                        key=f"final_qty_{r['psm_id']}",
                        help="System recommendation shown above. Change if needed."
                    )

                    # Checkbox confirmation — operator must tick before PO is raised
                    confirmed = st.checkbox(
                        f"✅ Confirm — raise PO for **{final_qty} units** of "
                        f"{r['product_name']} {power_str} to {eff.get('supplier_name','supplier')}",
                        key=f"confirm_{r['psm_id']}"
                    )

                    if confirmed:
                        if st.button(
                            f"📦 Place Order — {final_qty} units",
                            type="primary",
                            use_container_width=True,
                            key=f"reorder_btn_{r['psm_id']}"
                        ):
                            if not eff.get("supplier_id"):
                                st.error("No supplier assigned.")
                            else:
                                exp = calculate_expected_delivery(
                                    eff["supplier_id"], _dt.datetime.now(),
                                    int(r["tat_days"] or 1)
                                )
                                res = create_po(
                                    source        = "MANUAL_REORDER",
                                    supplier_id   = eff["supplier_id"],
                                    supplier_name = eff["supplier_name"],
                                    items         = [POItem(
                                        product_id   = r["product_id"],
                                        product_name = r["product_name"],
                                        qty          = final_qty,
                                        notes        = (
                                            f"{power_str} | {smart['advisory_note']}"
                                        ),
                                    )],
                                    notes      = f"Manual reorder | Advisory: {smart['reorder_qty']} | Operator: {final_qty}",
                                    created_by = "purchase_ui",
                                )
                                if res.success:
                                    st.success(
                                        f"✅ PO {res.po_number} raised — "
                                        f"{final_qty} units → {eff['supplier_name']} | "
                                        f"Expected: {exp or '—'}"
                                    )
                                    st.rerun()
                                else:
                                    st.error(f"PO failed: {res.error}")

                except Exception as _ae:
                    # Fallback — plain button if advisory fails
                    st.warning(f"Advisory unavailable: {_ae}")
                    if st.button(f"📦 Raise PO",
                                 key=f"reorder_btn_fb_{r['psm_id']}",
                                 use_container_width=True):
                        st.info("Fix advisory error first.")

            elif is_low and not r["reorder_enabled"]:
                st.warning("⚠️ Below minimum but reorder not enabled. Enable in Stock Minimums tab.")
            elif has_open:
                st.info("📋 Open reorder PO already exists.")

def _render_supplier_override():
    """
    Manual supplier routing — when preferred supplier can't supply a specific
    product/power, operator sets an override to an alternate supplier.
    One active override per product. Full audit trail.
    """
    st.markdown("### 🔀 Supplier Override")
    st.caption(
        "Route a product to an alternate supplier when the preferred supplier "
        "cannot fulfil. Override stays active until manually cleared."
    )

    # ── Ensure supplier_product_override table exists ────────────────
    try:
        from modules.sql_adapter import run_write as _rw2
        _rw2("""
            CREATE TABLE IF NOT EXISTS supplier_product_override (
                id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id           UUID NOT NULL REFERENCES products(id),
                override_supplier_id UUID NOT NULL REFERENCES parties(id),
                reason               TEXT,
                is_active            BOOLEAN DEFAULT TRUE,
                created_by           TEXT,
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                updated_at           TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    except Exception:
        pass

    try:
        from modules.sql_adapter import run_query

        # Load active overrides
        overrides = run_query("""
            SELECT
                spo.id::text                        AS override_id,
                spo.product_id::text                AS product_id,
                p.product_name,
                COALESCE(p.brand, '')               AS brand,
                COALESCE(pref.party_name, '—')      AS preferred_supplier,
                alt.party_name                      AS override_supplier,
                alt.id::text                        AS override_supplier_id,
                spo.reason,
                spo.created_by,
                spo.created_at::text                AS created_at
            FROM supplier_product_override spo
            JOIN products p     ON p.id   = spo.product_id
            JOIN parties  alt   ON alt.id = spo.override_supplier_id
            LEFT JOIN parties pref ON pref.id = p.preferred_supplier_id
            WHERE spo.is_active = TRUE
            ORDER BY spo.created_at DESC
        """) or []

        # Load all products with preferred_supplier set (candidates for override)
        products = run_query("""
            SELECT
                p.id::text          AS product_id,
                p.product_name,
                COALESCE(p.brand, '') AS brand,
                sup.party_name      AS preferred_supplier
            FROM products p
            JOIN parties sup ON sup.id = p.preferred_supplier_id
            WHERE COALESCE(p.is_active, TRUE) = TRUE
            ORDER BY p.product_name
        """) or []

        # Load all active suppliers
        suppliers = run_query("""
            SELECT id::text AS supplier_id, party_name
            FROM parties
            WHERE UPPER(party_type) IN ('SUPPLIER', 'VENDOR')
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY party_name
        """) or []

    except Exception as e:
        st.error(f"Could not load override data: {e}")
        return

    # ── Active overrides table ────────────────────────────────────────
    if overrides:
        st.markdown(f"**{len(overrides)} active override(s):**")
        for ov in overrides:
            with st.expander(
                f"🔀  {ov['product_name']}  ·  {ov['brand']}  "
                f"→  {ov['override_supplier']}  "
                f"(was: {ov['preferred_supplier']})",
                expanded=False,
            ):
                st.markdown(
                    f"**Reason:** {ov.get('reason') or '—'}  \n"
                    f"**Set by:** {ov.get('created_by') or '—'}  \n"
                    f"**Set at:** {str(ov.get('created_at') or '')[:16]}"
                )
                if st.button(f"❌ Clear Override",
                             key=f"clear_ov_{ov['override_id']}",
                             use_container_width=True):
                    try:
                        from modules.procurement.po_engine import clear_supplier_override
                        if clear_supplier_override(ov["product_id"]):
                            st.success(f"✅ Override cleared — {ov['product_name']} reverts to preferred supplier")
                            st.rerun()
                        else:
                            st.error("Failed to clear override")
                    except Exception as e:
                        st.error(f"Error: {e}")
    else:
        st.info("No active overrides. All products are routing to their preferred supplier.")

    st.markdown("---")

    # ── Set new override ──────────────────────────────────────────────
    st.markdown("#### Set Override for a Product")

    if not products:
        st.info(
            "**No products have a Preferred Supplier assigned yet.**\n\n"
            "To set up supplier routing:\n"
            "1. Go to **Data Loader → Smart Import → Product Master**\n"
            "2. Download the current product master\n"
            "3. Fill in the **PreferredSupplier** column (use exact party name e.g. *Alcon India*)\n"
            "4. Set **AutoFulfillment = YES** for contact lenses and standard RX products\n"
            "5. Import back\n\n"
            "Once imported, products will appear here for override routing."
        )
        return
    if not suppliers:
        st.warning("No active suppliers found in party master.")
        return

    prod_labels = [f"{p['product_name']} · {p['brand']} (pref: {p['preferred_supplier']})"
                   for p in products]
    sup_labels  = [s["party_name"] for s in suppliers]

    col1, col2 = st.columns(2)
    with col1:
        sel_prod_idx = st.selectbox("Product", range(len(products)),
                                    format_func=lambda i: prod_labels[i],
                                    key="ov_product_sel")
    with col2:
        sel_sup_idx = st.selectbox("Route to Supplier", range(len(suppliers)),
                                   format_func=lambda i: sup_labels[i],
                                   key="ov_supplier_sel")

    reason = st.text_input("Reason (required)",
                           placeholder="e.g. Alcon out of -3.00 SPH, routing to local distributor",
                           key="ov_reason")

    if st.button("✅ Set Override", type="primary",
                 use_container_width=True, key="ov_set_btn"):
        if not reason.strip():
            st.error("Please enter a reason for the override.")
        else:
            try:
                from modules.procurement.po_engine import set_supplier_override
                pid = products[sel_prod_idx]["product_id"]
                sid = suppliers[sel_sup_idx]["supplier_id"]
                if set_supplier_override(pid, sid, reason.strip(), created_by="purchase_ui"):
                    st.success(
                        f"✅ Override set: {products[sel_prod_idx]['product_name']} "
                        f"→ {suppliers[sel_sup_idx]['party_name']}"
                    )
                    st.rerun()
                else:
                    st.error("Failed to set override.")
            except Exception as e:
                st.error(f"Error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 6 — SUPPLIER SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────

def _render_supplier_schedule():
    """
    Set order cutoff time and closed days per supplier.
    These feed into calculate_expected_delivery() for accurate TAT.
    """
    st.markdown("### ⏰ Supplier Schedule")
    st.caption(
        "Configure when each supplier accepts orders and which days they are closed. "
        "Used to calculate accurate expected delivery dates."
    )

    try:
        from modules.sql_adapter import run_query, run_write

        suppliers = run_query("""
            SELECT
                id::text                                        AS supplier_id,
                party_name,
                COALESCE(supplier_closed_days, ARRAY[]::text[]) AS closed_days,
                order_cutoff_time::text                         AS cutoff_time
            FROM parties
            WHERE UPPER(party_type) IN ('SUPPLIER','VENDOR')
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY party_name
        """) or []
    except Exception as e:
        st.error(f"Could not load supplier data: {e}")
        return

    if not suppliers:
        st.info("No active suppliers found.")
        return

    _days_of_week = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    for sup in suppliers:
        with st.expander(
            f"🏢  {sup['party_name']}  "
            f"| Cutoff: {sup.get('cutoff_time') or '—'}  "
            f"| Closed: {', '.join(sup.get('closed_days') or []) or 'None'}",
            expanded=False,
        ):
            col1, col2 = st.columns(2)

            with col1:
                current_cutoff = str(sup.get("cutoff_time") or "15:00")[:5]
                new_cutoff = st.text_input(
                    "Order Cutoff Time (HH:MM)",
                    value=current_cutoff,
                    key=f"cutoff_{sup['supplier_id']}",
                    help="Orders placed after this time count as next working day"
                )

            with col2:
                current_closed = list(sup.get("closed_days") or [])
                new_closed = st.multiselect(
                    "Closed Days",
                    _days_of_week,
                    default=[d for d in current_closed if d in _days_of_week],
                    key=f"closed_{sup['supplier_id']}",
                )

            if st.button(f"💾 Save Schedule",
                         key=f"save_sched_{sup['supplier_id']}",
                         use_container_width=True):
                try:
                    # Validate cutoff format
                    parts = new_cutoff.strip().split(":")
                    if len(parts) != 2 or not (0 <= int(parts[0]) <= 23) \
                            or not (0 <= int(parts[1]) <= 59):
                        st.error("Invalid time format. Use HH:MM e.g. 15:00")
                    else:
                        run_write("""
                            UPDATE parties
                               SET supplier_closed_days = %(closed)s,
                                   order_cutoff_time    = %(cutoff)s
                             WHERE id = %(sid)s::uuid
                        """, {
                            "sid":    sup["supplier_id"],
                            "closed": new_closed if new_closed else None,
                            "cutoff": f"{new_cutoff.strip()}:00",
                        })
                        st.success(
                            f"✅ Schedule saved for {sup['party_name']} — "
                            f"Cutoff: {new_cutoff} | Closed: {', '.join(new_closed) or 'None'}"
                        )
                        st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 7 — STOCK MINIMUMS MANAGER
# ─────────────────────────────────────────────────────────────────────────────

def _render_stock_minimums():
    """
    Manage product_stock_minimum table.
    Power-wise minimum stock rules — quantities checked across all batches combined.

    Three sections:
    1. Existing rules — edit min_qty, reorder_qty, auto_fulfillment, reorder_enabled
    2. Add new rule — select product + enter power + set quantities
    3. Auto-suggest from velocity — pre-fill based on 90-day sales history
    """
    st.markdown("### 📐 Stock Minimums")
    st.caption(
        "Define minimum stock per product + power. "
        "System checks combined quantity across ALL batches for that power. "
        "Set **ReorderEnabled = YES** to auto-raise POs. "
        "Set **AutoFulfillment = YES** for contact lenses and standard RX lenses."
    )

    try:
        from modules.sql_adapter import run_query, run_write

        # Load existing rules
        rules = run_query("""
            SELECT
                psm.id::text                    AS id,
                psm.product_id::text            AS product_id,
                p.product_name,
                COALESCE(p.brand,'')            AS brand,
                COALESCE(p.main_group,'')       AS main_group,
                psm.sph, psm.cyl, psm.axis, psm.add_power, psm.eye_side,
                psm.min_qty, psm.reorder_qty,
                psm.auto_fulfillment,
                psm.reorder_enabled,
                psm.system_suggested_min,
                psm.suggested_reorder_qty,
                psm.avg_daily_sales,
                psm.last_advisory_at,
                psm.auto_order_enabled,
                COALESCE(SUM(i.quantity), 0)    AS current_stock
            FROM product_stock_minimum psm
            JOIN products p ON p.id = psm.product_id
            LEFT JOIN inventory_stock i
                   ON i.product_id = psm.product_id
                  AND COALESCE(i.sph,       0) = COALESCE(psm.sph,       0)
                  AND COALESCE(i.cyl,       0) = COALESCE(psm.cyl,       0)
                  AND COALESCE(i.axis,      0) = COALESCE(psm.axis,      0)
                  AND COALESCE(i.add_power, 0) = COALESCE(psm.add_power, 0)
                  AND COALESCE(i.is_active, TRUE) = TRUE
            GROUP BY psm.id, psm.product_id, p.product_name, p.brand,
                     p.main_group, psm.sph, psm.cyl, psm.axis,
                     psm.add_power, psm.eye_side, psm.min_qty,
                     psm.reorder_qty, psm.auto_fulfillment, psm.reorder_enabled,
                     psm.system_suggested_min, psm.suggested_reorder_qty,
                     psm.avg_daily_sales, psm.last_advisory_at,
                     psm.auto_order_enabled
            ORDER BY p.product_name, psm.sph, psm.cyl
        """) or []

        # Load products with preferred supplier for add-rule form
        products = run_query("""
            SELECT id::text AS product_id, product_name,
                   COALESCE(brand,'') AS brand,
                   COALESCE(main_group,'') AS main_group
            FROM products
            WHERE COALESCE(is_active, TRUE) = TRUE
              AND preferred_supplier_id IS NOT NULL
            ORDER BY product_name
        """) or []

    except Exception as e:
        st.error(f"Could not load data: {e}")
        return

    # ── Section 1: Existing rules ─────────────────────────────────────
    if rules:
        st.markdown(f"#### Existing Rules ({len(rules)})")

        # Group by product for cleaner display
        from itertools import groupby
        rules_sorted = sorted(rules, key=lambda r: r["product_name"])

        for pname, grp in groupby(rules_sorted, key=lambda r: r["product_name"]):
            plist = list(grp)
            brand = plist[0]["brand"]
            mg    = plist[0]["main_group"]

            with st.expander(f"📦 {pname} · {brand}  ({len(plist)} rules)", expanded=False):
                for r in plist:
                    pp = []
                    if r.get("sph")       is not None: pp.append(f"SPH {float(r['sph']):+.2f}")
                    if r.get("cyl")       is not None: pp.append(f"CYL {float(r['cyl']):+.2f}")
                    if r.get("axis")      is not None: pp.append(f"AX {int(r['axis'])}")
                    if r.get("add_power") is not None: pp.append(f"ADD {float(r['add_power']):+.2f}")
                    if r.get("eye_side"):               pp.append(f"Eye:{r['eye_side']}")
                    power_lbl = " | ".join(pp) if pp else "All / No power"

                    stock_now = float(r["current_stock"])
                    minq      = float(r["min_qty"])
                    is_low    = stock_now < minq

                    st.markdown(
                        f"**{power_lbl}**  "
                        f"{'🔴' if is_low else '✅'}  "
                        f"Stock: {stock_now:.0f} / Min: {minq:.0f}"
                    )

                    # Advisory info
                    adv_last = str(r.get("last_advisory_at") or "")[:16]
                    sugg_min = r.get("system_suggested_min")
                    sugg_rq  = r.get("suggested_reorder_qty")
                    avg_day  = float(r.get("avg_daily_sales") or 0)
                    if sugg_min or sugg_rq:
                        st.markdown(
                            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                            f"border-radius:6px;padding:6px 12px;font-size:0.75rem;"
                            f"margin-bottom:6px'>"
                            f"🤖 <span style='color:#60a5fa'>Advisory</span>  "
                            f"Suggested Min: <b style='color:#fbbf24'>{sugg_min or '—'}</b>  |  "
                            f"Suggested Reorder: <b style='color:#a78bfa'>{sugg_rq or '—'}</b>  |  "
                            f"Avg/day: <b style='color:#10b981'>{avg_day:.2f}</b>  |  "
                            f"<span style='color:#475569'>Last run: {adv_last or 'never'}</span>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

                    c1, c2, c3, c4, c5 = st.columns(5)
                    new_min   = c1.number_input("Min Qty",     min_value=0, value=int(r["min_qty"]),
                                                key=f"min_{r['id']}")
                    new_reord_en   = c2.checkbox("Reorder On",   value=bool(r["reorder_enabled"]),
                                                 key=f"ren_{r['id']}")
                    new_auto       = c3.checkbox("Auto-Bill",    value=bool(r["auto_fulfillment"]),
                                                 key=f"auto_{r['id']}")
                    new_auto_order = c4.checkbox("🤖 Auto Order",
                                                 value=bool(r.get("auto_order_enabled")),
                                                 key=f"ao_{r['id']}",
                                                 help="When enabled, PO is raised automatically without operator confirmation. Enable only after data is mature.")

                    with c5:
                        st.write("")
                        col_save, col_del = st.columns(2)
                        if col_save.button("💾", key=f"save_{r['id']}",
                                           help="Save changes", use_container_width=True):
                            run_write("""
                                UPDATE product_stock_minimum
                                   SET min_qty           = %(min)s,
                                       reorder_enabled   = %(ren)s,
                                       auto_fulfillment  = %(auto)s,
                                       auto_order_enabled= %(ao)s,
                                       updated_at        = NOW()
                                 WHERE id = %(id)s::uuid
                            """, {
                                "id": r["id"], "min": new_min,
                                "ren": new_reord_en,
                                "auto": new_auto, "ao": new_auto_order,
                            })
                            st.success("Saved")
                            st.rerun()
                        if col_del.button("🗑️", key=f"del_{r['id']}",
                                          help="Delete rule", use_container_width=True):
                            run_write(
                                "DELETE FROM product_stock_minimum WHERE id = %(id)s::uuid",
                                {"id": r["id"]}
                            )
                            st.rerun()
                    st.markdown("---")
    else:
        st.info("No rules yet. Add below or use Auto-Suggest.")

    # ── Section 2: Add new rule ───────────────────────────────────────
    st.markdown("#### ➕ Add New Rule")

    if not products:
        st.warning(
            "No products with a preferred supplier found. "
            "Assign PreferredSupplier in the product loader first."
        )
        return

    prod_labels = [f"{p['product_name']} · {p['brand']} · {p['main_group']}"
                   for p in products]

    col_p, col_eye, col_mode = st.columns([3, 1, 1])
    with col_p:
        sel_idx = st.selectbox("Product", range(len(products)),
                               format_func=lambda i: prod_labels[i],
                               key="psm_prod_sel")
    with col_eye:
        eye_side = st.selectbox("Eye Side", ["B","R","L"],
                                key="psm_eye_sel",
                                help="B=Both, R=Right, L=Left")
    with col_mode:
        range_mode = st.checkbox("📐 Range Mode",
                                 key="psm_range_mode",
                                 help="Add rules for a full power range in one go")

    # ── Shared qty / flags ────────────────────────────────────────────
    c5, c6, c7 = st.columns(3)
    min_q = c5.number_input("Min Qty",      min_value=0, value=6,  key="psm_min",
                             help="Minimum combined stock across all batches")
    ren   = c6.checkbox("Reorder Enabled",  value=True,             key="psm_ren")
    af    = c7.checkbox("Auto Fulfillment", value=False,            key="psm_af")

    if af:
        sel_mg = products[sel_idx]["main_group"].lower()
        if not any(k in sel_mg for k in ("contact","lens","ophthalmic","spectacle")):
            st.warning(
                "⚠️ Auto Fulfillment is designed for contact lenses and standard RX lenses. "
                f"This product is '{products[sel_idx]['main_group']}' — are you sure?"
            )

    pid = products[sel_idx]["product_id"]

    if not range_mode:
        # ── SINGLE POWER MODE ─────────────────────────────────────────
        c1, c2, c3, c4 = st.columns(4)
        sph  = c1.number_input("SPH",  step=0.25, format="%.2f", value=0.0, key="psm_sph")
        cyl  = c2.number_input("CYL",  step=0.25, format="%.2f", value=0.0, key="psm_cyl")
        axis = c3.number_input("AXIS", step=1, min_value=0, max_value=180, value=0, key="psm_ax")
        add  = c4.number_input("ADD",  step=0.25, format="%.2f", value=0.0, key="psm_add")

        if st.button("✅ Add Rule", type="primary", use_container_width=True, key="psm_add_btn"):
            try:
                run_write("""
                    INSERT INTO product_stock_minimum
                      (product_id, sph, cyl, axis, add_power, eye_side,
                       min_qty, auto_fulfillment, reorder_enabled,
                       created_at, updated_at)
                    VALUES
                      (%(pid)s::uuid,
                       NULLIF(%(sph)s, 0), NULLIF(%(cyl)s, 0),
                       NULLIF(%(axis)s, 0), NULLIF(%(add)s, 0),
                       %(eye)s, %(min)s, %(af)s, %(ren)s,
                       NOW(), NOW())
                    ON CONFLICT (product_id, sph, cyl, axis, add_power, eye_side)
                    DO UPDATE SET
                        min_qty          = EXCLUDED.min_qty,
                        auto_fulfillment = EXCLUDED.auto_fulfillment,
                        reorder_enabled  = EXCLUDED.reorder_enabled,
                        updated_at       = NOW()
                """, {
                    "pid": pid, "sph": sph, "cyl": cyl,
                    "axis": axis, "add": add, "eye": eye_side,
                    "min": min_q, "af": af, "ren": ren,
                })
                st.success(
                    f"✅ Rule added: {products[sel_idx]['product_name']} "
                    f"SPH {sph:+.2f} — Min: {min_q}"
                )
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    else:
        # ── RANGE MODE ────────────────────────────────────────────────
        st.markdown(
            "<div style='background:#0d1a2e;border:1px solid #3b82f6;"
            "border-radius:8px;padding:10px 14px;margin-bottom:10px'>"
            "<b style='color:#60a5fa'>📐 Range Mode</b>"
            "<span style='color:#94a3b8;font-size:0.8rem;margin-left:8px'>"
            "Generates one rule per power step across the range. "
            "Same min/reorder qty applied to all powers.</span></div>",
            unsafe_allow_html=True
        )

        # SPH range
        st.markdown("**SPH Range**")
        rs1, rs2, rs3 = st.columns(3)
        sph_from = rs1.number_input("SPH From", step=0.25, format="%.2f",
                                     value=-6.0, key="psm_sph_from")
        sph_to   = rs2.number_input("SPH To",   step=0.25, format="%.2f",
                                     value=0.0,  key="psm_sph_to")
        sph_step = rs3.number_input("SPH Step",  step=0.25, format="%.2f",
                                     value=0.25, min_value=0.25, key="psm_sph_step")

        # CYL range (optional)
        st.markdown("**CYL Range** *(leave both 0.00 to skip CYL)*")
        rc1, rc2, rc3 = st.columns(3)
        cyl_from = rc1.number_input("CYL From", step=0.25, format="%.2f",
                                     value=0.0, key="psm_cyl_from")
        cyl_to   = rc2.number_input("CYL To",   step=0.25, format="%.2f",
                                     value=0.0, key="psm_cyl_to")
        cyl_step = rc3.number_input("CYL Step", step=0.25, format="%.2f",
                                     value=0.25, min_value=0.25, key="psm_cyl_step")

        # AXIS — fixed value when CYL is used
        ra1, ra2 = st.columns(2)
        axis_val = ra1.number_input("AXIS (fixed, only if CYL used)",
                                     step=1, min_value=0, max_value=180,
                                     value=0, key="psm_axis_fixed")
        add_val  = ra2.number_input("ADD (fixed, 0 = not applicable)",
                                     step=0.25, format="%.2f",
                                     value=0.0, key="psm_add_fixed")

        # Preview count
        def _gen_range(from_v, to_v, step_v):
            """Generate values from_v to to_v inclusive at step_v intervals."""
            import math
            vals = []
            if step_v <= 0:
                return vals
            # Handle both positive and negative ranges
            lo, hi = min(from_v, to_v), max(from_v, to_v)
            n = int(round((hi - lo) / step_v)) + 1
            for i in range(n):
                v = round(lo + i * step_v, 2)
                if v <= hi + 0.001:
                    vals.append(v)
            return vals

        sph_vals = _gen_range(sph_from, sph_to, sph_step)
        cyl_vals = _gen_range(cyl_from, cyl_to, cyl_step) if (cyl_from != 0 or cyl_to != 0) else [0.0]
        total_rules = len(sph_vals) * len(cyl_vals)

        if total_rules == 0:
            st.warning("⚠️ No values in range — check From/To/Step values.")

        elif total_rules > 200:
            st.warning(f"⚠️ {total_rules} rules is a lot — consider narrowing the range.")

        else:
            st.info(
                f"📋 Will generate **{total_rules} rules** "
                f"({len(sph_vals)} SPH × {len(cyl_vals)} CYL values)"
            )

        # ── Preview table ─────────────────────────────────────────────
        if total_rules > 0:
            show_preview = st.checkbox(
                f"👁️ Preview all {total_rules} rules before adding",
                key="psm_preview_toggle"
            )
            if show_preview:
                import pandas as _pd
                preview_rows = []
                for _sv in sph_vals:
                    for _cv in cyl_vals:
                        _ax = axis_val if _cv != 0.0 else 0
                        _ad = add_val  if add_val != 0.0 else None
                        preview_rows.append({
                            "SPH":        f"{_sv:+.2f}",
                            "CYL":        f"{_cv:+.2f}" if _cv != 0.0 else "—",
                            "AXIS":       str(_ax) if _ax else "—",
                            "ADD":        f"{_ad:+.2f}" if _ad else "—",
                            "Eye":        eye_side,
                            "Min Qty":    min_q,
                            "Reorder En": "✅" if ren else "—",
                            "Auto-Bill":  "✅" if af  else "—",
                        })
                _prev_df = _pd.DataFrame(preview_rows)
                st.dataframe(
                    _prev_df,
                    use_container_width=True,
                    hide_index=True,
                    height=min(400, (total_rules + 1) * 36),
                )
                st.caption(
                    f"All {total_rules} rows above will be inserted / updated "
                    f"on **{products[sel_idx]['product_name']}**. "
                    f"Existing rules for the same power will be updated."
                )

        btn_label = f"✅ Add {total_rules} Rules" if total_rules > 0 else "✅ Add Rules"
        if st.button(btn_label, type="primary", use_container_width=True,
                     key="psm_range_add_btn", disabled=total_rules == 0):
            added = 0
            skipped = 0
            errors = []
            for sph_v in sph_vals:
                for cyl_v in cyl_vals:
                    # Only set axis if CYL is non-zero
                    ax = axis_val if cyl_v != 0.0 else 0
                    ad = add_val if add_val != 0.0 else None
                    try:
                        run_write("""
                            INSERT INTO product_stock_minimum
                              (product_id, sph, cyl, axis, add_power, eye_side,
                               min_qty, auto_fulfillment, reorder_enabled,
                               created_at, updated_at)
                            VALUES
                              (%(pid)s::uuid,
                               NULLIF(%(sph)s::numeric, 0),
                               NULLIF(%(cyl)s::numeric, 0),
                               NULLIF(%(axis)s::integer, 0),
                               %(add)s,
                               %(eye)s, %(min)s, %(af)s, %(ren)s,
                               NOW(), NOW())
                            ON CONFLICT (product_id, sph, cyl, axis, add_power, eye_side)
                            DO UPDATE SET
                                min_qty          = EXCLUDED.min_qty,
                                auto_fulfillment = EXCLUDED.auto_fulfillment,
                                reorder_enabled  = EXCLUDED.reorder_enabled,
                                updated_at       = NOW()
                        """, {
                            "pid": pid, "sph": sph_v, "cyl": cyl_v,
                            "axis": ax, "add": ad, "eye": eye_side,
                            "min": min_q, "af": af, "ren": ren,
                        })
                        added += 1
                    except Exception as e:
                        skipped += 1
                        errors.append(str(e))

            if added:
                st.success(
                    f"✅ {added} rules added for "
                    f"{products[sel_idx]['product_name']} — "
                    f"SPH {sph_from:+.2f} to {sph_to:+.2f}"
                    + (f", CYL {cyl_from:+.2f} to {cyl_to:+.2f}" if len(cyl_vals) > 1 else "")
                )
            if skipped:
                st.warning(f"⚠️ {skipped} rules skipped. First error: {errors[0] if errors else ''}")
            if added:
                st.rerun()

    # ── Section 3: Auto-suggest from velocity ─────────────────────────
    st.markdown("---")
    st.markdown("#### 🤖 Auto-Suggest from Sales Velocity")
    st.caption(
        "Analyses 90-day sales history per product+power. "
        "Suggests min_qty = 30-day demand. Review and confirm before saving."
    )

    if st.button("📊 Generate Suggestions", key="psm_velocity_btn",
                 use_container_width=True):
        try:
            suggestions = run_query("""
                SELECT
                    ol.product_id::text                     AS product_id,
                    p.product_name,
                    COALESCE(p.brand,'')                    AS brand,
                    COALESCE(ol.sph, 0)                     AS sph,
                    COALESCE(ol.cyl, 0)                     AS cyl,
                    COALESCE(ol.axis, 0)                    AS axis,
                    COALESCE(ol.add_power, 0)               AS add_power,
                    COALESCE(ol.eye_side, 'B')              AS eye_side,
                    SUM(ol.quantity)                        AS total_90d,
                    ROUND(SUM(ol.quantity)::numeric / 3, 0) AS suggested_min,
                    ROUND(SUM(ol.quantity)::numeric / 3, 0) AS suggested_reorder
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                JOIN products p ON p.id = ol.product_id
                WHERE o.created_at >= NOW() - INTERVAL '90 days'
                  AND COALESCE(o.status,'') NOT IN ('CANCELLED','RETURNED')
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND p.preferred_supplier_id IS NOT NULL
                GROUP BY ol.product_id, p.product_name, p.brand,
                         ol.sph, ol.cyl, ol.axis, ol.add_power, ol.eye_side
                HAVING SUM(ol.quantity) > 0
                ORDER BY total_90d DESC
                LIMIT 50
            """) or []

            if not suggestions:
                st.info("No sales data in last 90 days for products with preferred supplier.")
            else:
                st.markdown(f"**{len(suggestions)} suggestions** (top 50 by volume):")
                for s in suggestions:
                    pp = []
                    if s.get("sph"):  pp.append(f"SPH {float(s['sph']):+.2f}")
                    if s.get("cyl"):  pp.append(f"CYL {float(s['cyl']):+.2f}")
                    if s.get("axis"): pp.append(f"AX {int(s['axis'])}")
                    if s.get("add_power"): pp.append(f"ADD {float(s['add_power']):+.2f}")
                    power_lbl = " | ".join(pp) if pp else "No power"

                    col_l, col_r = st.columns([3, 1])
                    col_l.markdown(
                        f"**{s['product_name']}** · {s['brand']}  \n"
                        f"{power_lbl} | 90d sold: {s['total_90d']} | "
                        f"Suggested min: **{int(s['suggested_min'])}**"
                    )
                    if col_r.button("➕ Add",
                                    key=f"sug_{s['product_id']}_{s['sph']}_{s['cyl']}",
                                    use_container_width=True):
                        try:
                            run_write("""
                                INSERT INTO product_stock_minimum
                                  (product_id, sph, cyl, axis, add_power, eye_side,
                                   min_qty, reorder_enabled,
                                   auto_fulfillment, created_at, updated_at)
                                VALUES
                                  (%(pid)s::uuid,
                                   NULLIF(%(sph)s::numeric, 0),
                                   NULLIF(%(cyl)s::numeric, 0),
                                   NULLIF(%(axis)s::integer, 0),
                                   NULLIF(%(add)s::numeric, 0),
                                   %(eye)s, %(min)s, TRUE, FALSE,
                                   NOW(), NOW())
                                ON CONFLICT (product_id, sph, cyl, axis, add_power, eye_side)
                                DO NOTHING
                            """, {
                                "pid":  s["product_id"],
                                "sph":  s["sph"],  "cyl": s["cyl"],
                                "axis": s["axis"], "add": s["add_power"],
                                "eye":  s["eye_side"],
                                "min":  int(s["suggested_min"]),
                            })
                            st.success(f"✅ Rule added for {s['product_name']} {power_lbl}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")
        except Exception as e:
            st.error(f"Velocity query failed: {e}")
