"""
procurement/advisory/advisory_service.py
=========================================
Advisory Procurement Service Layer.

ARCHITECTURE
------------
  advisory_panel.py → THIS FILE → sql_adapter
  Never: advisory_panel.py → sql_adapter directly.

WHAT THIS SERVICE OWNS
-----------------------
  - Loading advisory inventory with computed fields
  - Alert computation v2 (real reorder_min from DB, snooze support)
  - Advisory PO creation and tracking
  - Threshold CRUD
  - Smart PO bundling
"""

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional
import pandas as pd

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# GROUP CONFIG
# ═══════════════════════════════════════════════════════════════════════

ADVISORY_GROUPS: Dict[str, Dict] = {
    "Frames": {
        "main_groups":   ["frames", "spectacle frames", "sunglass frames"],
        "reorder_ratio": 1.0,
        "urgency_days":  7,
        "bundle":        True,
        "icon":          "🕶️",
    },
    "Solutions": {
        "main_groups":   ["solution", "contact lens solution", "cleaning"],
        "reorder_ratio": 2.0,
        "urgency_days":  3,
        "bundle":        True,
        "icon":          "🧴",
    },
    "Accessories": {
        "main_groups":   ["accessories", "cases", "cords", "cloths"],
        "reorder_ratio": 1.5,
        "urgency_days":  5,
        "bundle":        True,
        "icon":          "🎽",
    },
    "Semi-Finished Blanks": {
        "main_groups":   ["blanks", "semi-finished", "uncut"],
        "reorder_ratio": 1.0,
        "urgency_days":  7,
        "bundle":        False,
        "icon":          "🔵",
    },
}

# flat lookup: "spectacle frames" → "Frames"
_GROUP_MAP: Dict[str, str] = {
    mg.lower(): name
    for name, info in ADVISORY_GROUPS.items()
    for mg in info.get("main_groups", [])
}


# ═══════════════════════════════════════════════════════════════════════
# ZONE 1 STEP 1.1 — SMART ALERTS V2
# Uses real reorder_min from products table + snooze support
# ═══════════════════════════════════════════════════════════════════════

def build_smart_alerts_v2() -> List[Dict]:
    """
    Generate low-stock alerts using real reorder thresholds from DB.

    - Uses products.reorder_min (falls back to product_thresholds.min_stock)
    - Respects advisory_snoozed_until column
    - Deduplicates by product_id
    - Returns sorted: urgent → low → watch

    Returns:
        List of alert dicts with: product_id, name, stock_qty,
        reorder_min, urgency_tier, days_of_stock, suggested_reorder_qty,
        product_group, icon
    """
    today = date.today()

    try:
        from modules.sql_adapter import run_query

        rows = run_query("""
            SELECT
                p.product_id::text                          AS product_id,
                p.product_name                              AS name,
                COALESCE(inv.qty, 0)                        AS stock_qty,
                COALESCE(p.reorder_min,
                         pt.min_stock, 10)                  AS reorder_min,
                LOWER(p.main_group)                         AS main_group
            FROM products p
            LEFT JOIN product_thresholds pt ON pt.product_id = p.product_id
            LEFT JOIN (
                SELECT product_id, SUM(quantity) AS qty
                FROM inventory_batches
                WHERE status = 'ACTIVE'
                GROUP BY product_id
            ) inv ON inv.product_id = p.product_id
            WHERE COALESCE(inv.qty, 0) <=
                  COALESCE(p.reorder_min, pt.min_stock, 10)
              AND (
                    p.advisory_snoozed_until IS NULL
                    OR p.advisory_snoozed_until < %(today)s
                  )
        """, {"today": today}) or []

    except Exception as e:
        log.warning(f"[AdvisoryService] build_smart_alerts_v2 DB failed: {e}")
        return []

    seen: set = set()
    alerts: List[Dict] = []

    for r in rows:
        pid = r.get("product_id")
        if pid in seen:
            continue
        seen.add(pid)

        group  = _GROUP_MAP.get(str(r.get("main_group", "")).lower(), "Other")
        config = ADVISORY_GROUPS.get(group, {})
        urgency_days = int(config.get("urgency_days", 7))

        stock      = float(r.get("stock_qty", 0))
        reorder_min = float(r.get("reorder_min", 10))
        days_left  = max(0, int(stock))   # 1 unit ≈ 1 day; velocity added later

        if days_left <= 3:
            tier = "urgent"
        elif days_left <= urgency_days:
            tier = "low"
        else:
            tier = "watch"

        alerts.append({
            **r,
            "product_group":        group,
            "icon":                 config.get("icon", "📦"),
            "urgency_tier":         tier,
            "days_of_stock":        days_left,
            "suggested_reorder_qty": max(0, int(reorder_min - stock)) + int(reorder_min),
        })

    tier_order = {"urgent": 0, "low": 1, "watch": 2}
    alerts.sort(key=lambda x: (tier_order.get(x["urgency_tier"], 9), x["days_of_stock"]))
    return alerts


# ═══════════════════════════════════════════════════════════════════════
# INVENTORY LOADING
# ═══════════════════════════════════════════════════════════════════════

def load_advisory_inventory(selected_groups: List[str]) -> Optional[pd.DataFrame]:
    """Load advisory product inventory for given group names."""
    main_groups: List[str] = []
    for g in selected_groups:
        main_groups.extend(ADVISORY_GROUPS.get(g, {}).get("main_groups", []))

    if not main_groups:
        return pd.DataFrame()

    try:
        from modules.sql_adapter import run_query
        placeholders = ", ".join([f"%(g{i})s" for i in range(len(main_groups))])
        params = {f"g{i}": g for i, g in enumerate(main_groups)}

        rows = run_query(f"""
            SELECT
                p.product_id::text,
                p.product_name,
                p.brand,
                LOWER(p.main_group)                 AS main_group,
                COALESCE(pt.min_stock, 10)           AS min_stock,
                COALESCE(pt.max_stock, 50)           AS max_stock,
                COALESCE(inv.current_stock, 0)       AS current_stock,
                COALESCE(vel.velocity_per_day, 1.0)  AS velocity_per_day,
                sup.party_name                       AS preferred_supplier,
                sup.id::text                         AS preferred_supplier_id
            FROM products p
            LEFT JOIN product_thresholds pt
                   ON pt.product_id = p.product_id
            LEFT JOIN (
                SELECT product_id, SUM(quantity) AS current_stock
                FROM inventory_batches WHERE status = 'ACTIVE'
                GROUP BY product_id
            ) inv ON inv.product_id = p.product_id
            LEFT JOIN (
                SELECT soi.product_id,
                       COUNT(soi.id)::float /
                       GREATEST(EXTRACT(DAY FROM NOW() - MIN(so.created_at)), 1)
                           AS velocity_per_day
                FROM supplier_order_items soi
                JOIN supplier_orders so ON so.id = soi.supplier_order_id
                WHERE so.created_at >= NOW() - INTERVAL '90 days'
                GROUP BY soi.product_id
            ) vel ON vel.product_id = p.product_id
            LEFT JOIN parties sup ON sup.id = pt.preferred_supplier_id
            WHERE LOWER(p.main_group) IN ({placeholders})
            ORDER BY p.product_name
        """, params)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["product_group"] = df["main_group"].map(_GROUP_MAP).fillna("Other")
        return df

    except Exception as e:
        log.warning(f"[AdvisoryService] load_advisory_inventory failed: {e}")
        return None


def compute_alerts(inventory: pd.DataFrame) -> pd.DataFrame:
    """Compute alert rows from a loaded inventory DataFrame."""
    if inventory is None or inventory.empty:
        return pd.DataFrame()

    rows = []
    for _, row in inventory.iterrows():
        group  = row.get("product_group", "")
        config = ADVISORY_GROUPS.get(group, {})
        ratio       = float(config.get("reorder_ratio", 1.0))
        urgency_d   = int(config.get("urgency_days", 7))
        min_stock   = float(row.get("min_stock", 10))
        curr_stock  = float(row.get("current_stock", 0))
        velocity    = max(float(row.get("velocity_per_day", 1.0)), 0.01)
        threshold   = min_stock * ratio

        if curr_stock < threshold:
            days_left = int(curr_stock / velocity)
            suggested = max(0, int(min_stock - curr_stock)) + int(min_stock)
            tier = "urgent" if days_left <= 3 else ("low" if days_left <= urgency_d else "watch")
            rows.append({
                **row.to_dict(),
                "days_of_stock":         days_left,
                "suggested_reorder_qty": suggested,
                "reorder_threshold":     threshold,
                "urgency_tier":          tier,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("days_of_stock")


# ═══════════════════════════════════════════════════════════════════════
# SUPPLIER LOOKUP
# ═══════════════════════════════════════════════════════════════════════

def get_ranked_suppliers_for_product(product_id: str) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT p.id::text AS id, p.party_name AS name,
                   COUNT(soi.id) AS past_orders
            FROM parties p
            LEFT JOIN supplier_orders so ON so.supplier_id = p.id
            LEFT JOIN supplier_order_items soi
                   ON soi.supplier_order_id = so.id
                  AND soi.product_id::text = %(pid)s
            WHERE LOWER(COALESCE(p.roletype,'')) IN ('supplier','vendor')
              AND COALESCE(p.isactive, true) = true
            GROUP BY p.id, p.party_name
            ORDER BY past_orders DESC, p.party_name
        """, {"pid": str(product_id)}) or []
    except Exception as e:
        log.warning(f"[AdvisoryService] Supplier lookup failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# PO OPERATIONS
# ═══════════════════════════════════════════════════════════════════════

def create_quick_refill_po(
    product_id: str, product_name: str, qty: int,
    supplier_id: str, supplier_name: str, notes: str = "",
) -> Dict:
    try:
        from modules.procurement.po_engine import create_po, POItem, SOURCE_ADVISORY
        result = create_po(
            source=SOURCE_ADVISORY,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            items=[POItem(product_id=product_id, product_name=product_name, qty=qty)],
            notes=notes,
        )
        return {
            "success": result.success,
            "po_id":   result.po_number or "",
            "message": result.message,
            "error":   result.error,
        }
    except Exception as e:
        log.error(f"[AdvisoryService] Quick refill failed: {e}")
        return {"success": False, "error": str(e)}


def bundle_alerts_into_pos(alerts: pd.DataFrame) -> Dict[str, str]:
    """Create one PO per supplier from alert rows. Returns {supplier_name: po_number}."""
    if alerts.empty:
        return {}
    from modules.procurement.po_engine import create_po, POItem, SOURCE_ADVISORY
    results: Dict[str, str] = {}
    for supplier_id, group in alerts.groupby("preferred_supplier_id", dropna=False):
        supplier_name = group["preferred_supplier"].iloc[0] if "preferred_supplier" in group.columns else "Unknown"
        items = [
            POItem(
                product_id=str(r.get("product_id", "")),
                product_name=str(r.get("product_name", r.get("name", ""))),
                qty=int(r.get("suggested_reorder_qty", 1)),
            )
            for _, r in group.iterrows()
        ]
        try:
            result = create_po(
                source=SOURCE_ADVISORY,
                supplier_id=str(supplier_id) if supplier_id else "",
                supplier_name=supplier_name,
                items=items,
            )
            results[supplier_name] = result.po_number or "?"
        except Exception as e:
            log.error(f"[AdvisoryService] Bundle PO failed for {supplier_name}: {e}")
    return results


# ═══════════════════════════════════════════════════════════════════════
# THRESHOLD MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def load_products_for_group(group_name: str) -> Optional[pd.DataFrame]:
    main_groups = ADVISORY_GROUPS.get(group_name, {}).get("main_groups", [])
    if not main_groups:
        return pd.DataFrame()
    try:
        from modules.sql_adapter import run_query
        placeholders = ", ".join([f"%(g{i})s" for i in range(len(main_groups))])
        params = {f"g{i}": g for i, g in enumerate(main_groups)}
        rows = run_query(f"""
            SELECT p.product_id::text, p.product_name, p.brand,
                   COALESCE(pt.min_stock, 10) AS min_stock,
                   COALESCE(pt.max_stock, 50) AS max_stock,
                   COALESCE(inv.qty, 0)       AS current_stock
            FROM products p
            LEFT JOIN product_thresholds pt ON pt.product_id = p.product_id
            LEFT JOIN (
                SELECT product_id, SUM(quantity) AS qty
                FROM inventory_batches WHERE status = 'ACTIVE'
                GROUP BY product_id
            ) inv ON inv.product_id = p.product_id
            WHERE LOWER(p.main_group) IN ({placeholders})
            ORDER BY p.product_name
        """, params)
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        log.warning(f"[AdvisoryService] load_products_for_group failed: {e}")
        return None


def save_threshold(product_name: str, new_min: int, new_max: int) -> Dict:
    try:
        from modules.sql_adapter import run_query
        run_query("""
            INSERT INTO product_thresholds (product_id, min_stock, max_stock, updated_at)
            SELECT product_id, %(min)s, %(max)s, NOW()
            FROM products WHERE product_name = %(name)s
            ON CONFLICT (product_id) DO UPDATE
              SET min_stock = EXCLUDED.min_stock,
                  max_stock = EXCLUDED.max_stock,
                  updated_at = NOW()
        """, {"min": new_min, "max": new_max, "name": product_name})
        return {"success": True, "message": f"Saved — {product_name}: min={new_min}, max={new_max}"}
    except Exception as e:
        log.error(f"[AdvisoryService] save_threshold failed: {e}")
        return {"success": False, "error": str(e)}
