"""
procurement/advisory_service.py
=================================
Advisory Procurement Service Layer.

ARCHITECTURE (Issue 4 fix)
---------------------------
  BEFORE (wrong):  advisory_panel.py → sql_adapter directly
  AFTER  (correct): advisory_panel.py → advisory_service → sql_adapter

WHY THIS MATTERS
----------------
  - Advisory will grow fast (more product groups, smarter rules, ML signals)
  - UI should never know about SQL shapes
  - Service layer is testable without a database
  - Service layer can be called from API endpoints later
  - Business logic (velocity, urgency scoring) lives here, not in UI

WHAT THIS SERVICE OWNS
-----------------------
  - Loading advisory inventory with computed fields
  - Alert computation (urgency scoring, reorder suggestion)
  - Advisory PO creation and tracking
  - Threshold CRUD
  - Smart PO bundling logic

WHAT IT DOES NOT OWN
---------------------
  - Any st.* calls (those live in advisory_panel.py)
  - Raw SQL (delegates to sql_adapter)
"""

import logging
from typing import Dict, List, Optional
import pandas as pd

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# ADVISORY GROUP CONFIG  (moved from advisory_panel.py)
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

# Reverse lookup: main_group string → advisory group label
_GROUP_MAP: Dict[str, str] = {
    mg.lower(): group_name
    for group_name, info in ADVISORY_GROUPS.items()
    for mg in info.get("main_groups", [])
}


def get_group_config(group_name: str) -> Dict:
    """Return config dict for an advisory group. Returns {} if not found."""
    return ADVISORY_GROUPS.get(group_name, {})


# ═══════════════════════════════════════════════════════════════════════
# INVENTORY LOADING
# ═══════════════════════════════════════════════════════════════════════

def load_advisory_inventory(selected_groups: List[str]) -> Optional[pd.DataFrame]:
    """
    Load advisory product inventory for given groups.

    Returns DataFrame with columns:
      product_id, product_name, brand, main_group, product_group,
      current_stock, min_stock, max_stock, velocity_per_day,
      preferred_supplier, preferred_supplier_id

    Returns None on DB error.
    """
    all_main_groups: List[str] = []
    for g in selected_groups:
        all_main_groups.extend(ADVISORY_GROUPS.get(g, {}).get("main_groups", []))

    if not all_main_groups:
        return pd.DataFrame()

    try:
        from modules.sql_adapter import run_query

        placeholders = ", ".join([f"%(g{i})s" for i in range(len(all_main_groups))])
        params       = {f"g{i}": g for i, g in enumerate(all_main_groups)}

        rows = run_query(f"""
            SELECT
                p.product_id::text,
                p.product_name,
                p.brand,
                LOWER(p.main_group)                  AS main_group,
                COALESCE(pt.min_stock, 10)            AS min_stock,
                COALESCE(pt.max_stock, 50)            AS max_stock,
                COALESCE(inv.current_stock, 0)        AS current_stock,
                COALESCE(vel.velocity_per_day, 1.0)   AS velocity_per_day,
                sup.party_name                        AS preferred_supplier,
                sup.id::text                          AS preferred_supplier_id
            FROM products p
            LEFT JOIN product_thresholds pt  ON pt.product_id = p.product_id
            LEFT JOIN (
                SELECT product_id, SUM(quantity) AS current_stock
                FROM inventory_batches WHERE status = 'ACTIVE'
                GROUP BY product_id
            ) inv ON inv.product_id = p.product_id
            LEFT JOIN (
                SELECT
                    soi.product_id,
                    COUNT(soi.id)::float / GREATEST(
                        EXTRACT(DAY FROM NOW() - MIN(so.created_at)), 1
                    ) AS velocity_per_day
                FROM supplier_order_items soi
                JOIN supplier_orders so ON so.id = soi.supplier_order_id
                WHERE so.created_at >= NOW() - INTERVAL '90 days'
                GROUP BY soi.product_id
            ) vel ON vel.product_id = p.product_id
            LEFT JOIN parties sup
                   ON sup.id = pt.preferred_supplier_id
            WHERE LOWER(p.main_group) IN ({placeholders})
            ORDER BY p.product_name
        """, params)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["product_group"] = df["main_group"].map(_GROUP_MAP).fillna("Other")
        return df

    except Exception as e:
        log.warning(f"[AdvisoryService] Failed to load inventory: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# ALERT COMPUTATION  (business logic — kept in service, not UI)
# ═══════════════════════════════════════════════════════════════════════

def compute_alerts(inventory: pd.DataFrame) -> pd.DataFrame:
    """
    Compute alert rows from inventory DataFrame.

    Returns rows where current_stock < reorder_threshold,
    sorted by urgency (days of stock left, ascending).

    Adds columns:
      days_of_stock         : estimated days until stockout
      suggested_reorder_qty : how much to order
      reorder_threshold     : computed threshold (min_stock × ratio)
      urgency_tier          : "urgent" | "low" | "watch"
    """
    if inventory.empty:
        return pd.DataFrame()

    alert_rows = []

    for _, row in inventory.iterrows():
        group      = row.get("product_group", "")
        config     = ADVISORY_GROUPS.get(group, {})
        ratio      = float(config.get("reorder_ratio", 1.0))
        urgency_d  = int(config.get("urgency_days", 7))

        min_stock  = float(row.get("min_stock", 10))
        curr_stock = float(row.get("current_stock", 0))
        velocity   = max(float(row.get("velocity_per_day", 1.0)), 0.01)

        reorder_threshold = min_stock * ratio

        if curr_stock < reorder_threshold:
            days_left = int(curr_stock / velocity)
            suggested = max(0, int(min_stock - curr_stock)) + int(min_stock)

            if days_left <= 3:
                tier = "urgent"
            elif days_left <= urgency_d:
                tier = "low"
            else:
                tier = "watch"

            alert_rows.append({
                **row.to_dict(),
                "days_of_stock":         days_left,
                "suggested_reorder_qty": suggested,
                "reorder_threshold":     reorder_threshold,
                "urgency_tier":          tier,
            })

    if not alert_rows:
        return pd.DataFrame()

    return pd.DataFrame(alert_rows).sort_values("days_of_stock")


# ═══════════════════════════════════════════════════════════════════════
# SUPPLIER LOOKUP
# ═══════════════════════════════════════════════════════════════════════

def get_ranked_suppliers_for_product(product_id: str) -> List[Dict]:
    """
    Fetch suppliers ranked by historical usage for this product.
    Returns list of {id, name, past_orders}.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                p.id::text AS id,
                p.party_name AS name,
                COUNT(soi.id) AS past_orders
            FROM parties p
            LEFT JOIN supplier_orders so ON so.supplier_id = p.id
            LEFT JOIN supplier_order_items soi
                   ON soi.supplier_order_id = so.id
                  AND soi.product_id::text = %(pid)s
            WHERE LOWER(COALESCE(p.roletype,'')) IN ('supplier','vendor')
              AND COALESCE(p.isactive, true) = true
            GROUP BY p.id, p.party_name
            ORDER BY past_orders DESC, p.party_name ASC
        """, {"pid": str(product_id)})
        return rows or []
    except Exception as e:
        log.warning(f"[AdvisoryService] Supplier lookup failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# PO OPERATIONS
# ═══════════════════════════════════════════════════════════════════════

def load_advisory_pos(limit: int = 200) -> Optional[pd.DataFrame]:
    """Load open advisory purchase orders."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                so.id::text          AS po_id,
                so.po_number,
                so.created_at,
                p.party_name         AS supplier_name,
                soi.product_name,
                soi.quantity         AS qty_ordered,
                so.status,
                so.expected_delivery
            FROM supplier_orders so
            JOIN parties p       ON p.id = so.supplier_id
            JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            WHERE so.source = 'ADVISORY'
            ORDER BY so.created_at DESC
            LIMIT %(limit)s
        """, {"limit": limit})
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        log.warning(f"[AdvisoryService] PO load failed: {e}")
        return None


def create_quick_refill_po(
    product_id: str,
    product_name: str,
    qty: int,
    supplier_id: str,
    supplier_name: str,
    notes: str = "",
) -> Dict:
    """
    Create a Quick Refill PO in DB.

    Returns:
        {"success": True, "po_id": "...", "message": "..."}
        {"success": False, "error": "..."}
    """
    try:
        from modules.sql_adapter import run_query
        result = run_query("""
            WITH new_po AS (
                INSERT INTO supplier_orders
                  (supplier_id, status, source, notes, created_at)
                VALUES
                  (%(sid)s, 'Draft', 'ADVISORY', %(notes)s, NOW())
                RETURNING id
            )
            INSERT INTO supplier_order_items
              (supplier_order_id, product_id, product_name, quantity)
            SELECT id, %(pid)s, %(pname)s, %(qty)s FROM new_po
            RETURNING supplier_order_id::text AS po_id
        """, {
            "sid":   supplier_id,
            "notes": notes,
            "pid":   product_id,
            "pname": product_name,
            "qty":   qty,
        })
        po_id = result[0]["po_id"] if result else "unknown"
        return {
            "success": True,
            "po_id":   po_id,
            "message": f"Quick Refill PO #{po_id} created — {product_name} × {qty} for {supplier_name}",
        }
    except Exception as e:
        log.error(f"[AdvisoryService] Quick refill PO failed: {e}")
        return {"success": False, "error": str(e)}


def bundle_alerts_into_pos(alerts: pd.DataFrame) -> Dict:
    """
    Smart PO Bundling (blueprint Section 5).
    Groups alert items by preferred_supplier_id,
    creates one PO per supplier.

    Returns summary: {supplier_name: po_id, ...}
    """
    if alerts.empty:
        return {}

    results = {}

    # Group by preferred supplier
    grouped = alerts.groupby("preferred_supplier_id", dropna=False)

    for supplier_id, group in grouped:
        supplier_name = group["preferred_supplier"].iloc[0] if "preferred_supplier" in group.columns else "Unknown"
        items = group.to_dict("records")

        try:
            from modules.sql_adapter import run_query
            # Create PO
            po_result = run_query("""
                INSERT INTO supplier_orders (supplier_id, status, source, created_at)
                VALUES (%(sid)s, 'Draft', 'ADVISORY', NOW())
                RETURNING id::text AS po_id
            """, {"sid": str(supplier_id) if supplier_id else None})

            if not po_result:
                continue

            po_id = po_result[0]["po_id"]

            # Insert all items
            for item in items:
                run_query("""
                    INSERT INTO supplier_order_items
                      (supplier_order_id, product_id, product_name, quantity)
                    VALUES (%(po_id)s, %(pid)s, %(pname)s, %(qty)s)
                """, {
                    "po_id": po_id,
                    "pid":   item.get("product_id"),
                    "pname": item.get("product_name"),
                    "qty":   item.get("suggested_reorder_qty", 1),
                })

            results[supplier_name] = po_id
            log.info(f"[AdvisoryService] Bundled PO {po_id} for {supplier_name} ({len(items)} items)")

        except Exception as e:
            log.error(f"[AdvisoryService] Bundle PO failed for {supplier_name}: {e}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# THRESHOLD MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def load_products_for_group(group_name: str) -> Optional[pd.DataFrame]:
    """Load products and their thresholds for a given advisory group."""
    main_groups = ADVISORY_GROUPS.get(group_name, {}).get("main_groups", [])
    if not main_groups:
        return pd.DataFrame()

    try:
        from modules.sql_adapter import run_query
        placeholders = ", ".join([f"%(g{i})s" for i in range(len(main_groups))])
        params       = {f"g{i}": g for i, g in enumerate(main_groups)}

        rows = run_query(f"""
            SELECT
                p.product_id::text,
                p.product_name,
                p.brand,
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
        log.warning(f"[AdvisoryService] Products for group failed: {e}")
        return None


def save_threshold(product_name: str, new_min: int, new_max: int) -> Dict:
    """
    Upsert product threshold.
    Returns {"success": bool, "message": str}.
    """
    try:
        from modules.sql_adapter import run_query
        run_query("""
            INSERT INTO product_thresholds (product_id, min_stock, max_stock, updated_at)
            SELECT product_id, %(min)s, %(max)s, NOW()
            FROM products WHERE product_name = %(name)s
            ON CONFLICT (product_id) DO UPDATE
              SET min_stock  = EXCLUDED.min_stock,
                  max_stock  = EXCLUDED.max_stock,
                  updated_at = NOW()
        """, {"min": new_min, "max": new_max, "name": product_name})
        return {
            "success": True,
            "message": f"Threshold saved — {product_name}: min={new_min}, max={new_max}",
        }
    except Exception as e:
        log.error(f"[AdvisoryService] Save threshold failed: {e}")
        return {"success": False, "error": str(e)}
