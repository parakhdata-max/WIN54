"""
suppliers/intelligence.py
===========================
Supplier Intelligence Layer — Priority 2.

Canonical location: modules/suppliers/intelligence.py
Also importable via: modules.procurement.supplier_intelligence (shim kept for compat)

WHAT THIS MODULE DOES
---------------------
  Scores every supplier on 4 axes and produces a ranked list
  with an overall reliability percentage.

  Factors:
    1. delivery_score   — avg days to deliver (lower = better)
    2. price_score      — relative unit cost vs category average
    3. rejection_score  — % of items rejected / returned
    4. reliability_score — PO confirmation rate + consistency

  Output example:
    [
      {"id": "SUP001", "name": "Shamir",       "score": 92, "grade": "A"},
      {"id": "SUP002", "name": "Essilor",      "score": 88, "grade": "A"},
      {"id": "SUP003", "name": "Local Lab",    "score": 74, "grade": "B"},
    ]

ARCHITECTURE
------------
  advisory_panel.py / fulfillment UI
      ↓
  supplier_intelligence.py  (THIS FILE — pure logic + DB)
      ↓
  sql_adapter

  No st.* calls here. Call from UI panels only.

PUBLIC API
----------
  get_scored_suppliers(product_group=None, limit=20)
      → list of scored supplier dicts

  get_ranked_suppliers_for_assignment(product_id, order_type=None)
      → list with score context — used in assignment panel

  get_supplier_scorecard(supplier_id)
      → full scorecard dict for one supplier
"""

import logging
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ── Scoring weights (must sum to 1.0) ────────────────────────────────
SCORE_WEIGHTS = {
    "delivery":    0.35,   # speed matters most
    "price":       0.25,   # competitive pricing
    "rejection":   0.25,   # quality / rejection rate
    "reliability": 0.15,   # PO confirmation consistency
}

# ── Grade thresholds ──────────────────────────────────────────────────
GRADE_MAP = [
    (90, "A+"),
    (80, "A"),
    (70, "B"),
    (60, "C"),
    (0,  "D"),
]


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════

def get_scored_suppliers(
    product_group: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    """
    Return all active suppliers scored and ranked.

    Each dict:
      id, name, score (0-100), grade, delivery_days_avg,
      rejection_pct, price_index, po_count, rank
    """
    raw = _load_supplier_metrics(product_group=product_group, limit=limit)
    if not raw:
        return []

    scored = [_score_supplier(s) for s in raw]
    scored.sort(key=lambda x: x["score"], reverse=True)

    for rank, s in enumerate(scored, 1):
        s["rank"] = rank

    return scored


def get_ranked_suppliers_for_assignment(
    product_id: str,
    order_type: Optional[str] = None,
) -> List[Dict]:
    """
    For the assignment panel — suppliers that have handled this product,
    ranked by score, with context fields the UI can display.

    Returns:
      id, name, score, grade, delivery_days_avg, past_orders_for_product
    """
    raw = _load_supplier_metrics_for_product(product_id)
    if not raw:
        # fallback: return all suppliers with no product context
        return get_scored_suppliers()

    scored = [_score_supplier(s) for s in raw]
    scored.sort(key=lambda x: x["score"], reverse=True)
    for rank, s in enumerate(scored, 1):
        s["rank"] = rank
    return scored


def get_supplier_scorecard(supplier_id: str) -> Optional[Dict]:
    """
    Full scorecard for a single supplier.
    Used in supplier performance dashboard.
    """
    raw = _load_supplier_metrics(supplier_id=supplier_id)
    if not raw:
        return None
    return _score_supplier(raw[0])


# ═══════════════════════════════════════════════════════════════════════
# SCORING ENGINE  (pure logic — no DB calls)
# ═══════════════════════════════════════════════════════════════════════

def _score_supplier(row: Dict) -> Dict:
    """
    Convert raw metrics into a 0-100 score + grade.
    All sub-scores normalised 0-100 before weighting.
    """
    delivery_raw    = float(row.get("delivery_days_avg") or 7)
    rejection_raw   = float(row.get("rejection_pct") or 0)
    price_index     = float(row.get("price_index") or 1.0)
    reliability_raw = float(row.get("reliability_pct") or 80)

    # Delivery: 1 day = 100, 14+ days = 0
    delivery_score = max(0.0, 100.0 - (delivery_raw - 1) * (100 / 13))

    # Rejection: 0% = 100, 20%+ = 0
    rejection_score = max(0.0, 100.0 - rejection_raw * 5)

    # Price: index 0.8 = 100, index 1.2 = 0  (lower price = higher score)
    price_score = max(0.0, min(100.0, (1.2 - price_index) / 0.4 * 100))

    # Reliability: direct percentage
    reliability_score = max(0.0, min(100.0, reliability_raw))

    composite = (
        delivery_score    * SCORE_WEIGHTS["delivery"]
        + price_score     * SCORE_WEIGHTS["price"]
        + rejection_score * SCORE_WEIGHTS["rejection"]
        + reliability_score * SCORE_WEIGHTS["reliability"]
    )
    score = round(composite)
    grade = next(g for threshold, g in GRADE_MAP if score >= threshold)

    return {
        **row,
        "score":             score,
        "grade":             grade,
        "delivery_score":    round(delivery_score),
        "rejection_score":   round(rejection_score),
        "price_score":       round(price_score),
        "reliability_score": round(reliability_score),
    }


# ═══════════════════════════════════════════════════════════════════════
# DATA LOADERS  (DB calls isolated here)
# ═══════════════════════════════════════════════════════════════════════

def _load_supplier_metrics(
    product_group: Optional[str] = None,
    supplier_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """
    Load raw supplier metrics from DB.
    Returns empty list on any failure.
    """
    try:
        from modules.sql_adapter import run_query

        where_clauses = ["LOWER(COALESCE(p.roletype,'')) IN ('supplier','vendor')",
                         "COALESCE(p.isactive, true) = true"]
        params: Dict = {"limit": limit}

        if supplier_id:
            where_clauses.append("p.id::text = %(supplier_id)s")
            params["supplier_id"] = supplier_id

        where_sql = " AND ".join(where_clauses)

        rows = run_query(f"""
            SELECT
                p.id::text                                          AS id,
                p.party_name                                        AS name,

                -- Delivery
                COALESCE(
                    AVG(
                        EXTRACT(DAY FROM
                            (COALESCE(so.received_at, so.updated_at) - so.created_at)
                        )
                    ), 7
                )::float                                            AS delivery_days_avg,

                -- Rejection
                COALESCE(
                    100.0 * SUM(CASE WHEN so.status = 'REJECTED' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(so.id), 0),
                    0
                )::float                                            AS rejection_pct,

                -- Price index (avg unit cost vs global avg)
                COALESCE(
                    AVG(soi.unit_price) /
                    NULLIF(
                        (SELECT AVG(unit_price) FROM supplier_order_items), 0
                    ), 1.0
                )::float                                            AS price_index,

                -- Reliability (% POs confirmed or received)
                COALESCE(
                    100.0 * SUM(
                        CASE WHEN so.status IN ('CONFIRMED','RECEIVED','COMPLETED')
                             THEN 1 ELSE 0 END
                    ) / NULLIF(COUNT(so.id), 0),
                    80
                )::float                                            AS reliability_pct,

                COUNT(so.id)::int                                   AS po_count

            FROM parties p
            LEFT JOIN supplier_orders so ON so.supplier_id = p.id
                AND so.created_at >= NOW() - INTERVAL '180 days'
            LEFT JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            WHERE {where_sql}
            GROUP BY p.id, p.party_name
            ORDER BY po_count DESC
            LIMIT %(limit)s
        """, params)

        return rows or []

    except Exception as e:
        log.warning(f"[SupplierIntelligence] Failed to load metrics: {e}")
        return []


def _load_supplier_metrics_for_product(product_id: str) -> List[Dict]:
    """Load metrics filtered to suppliers who have handled a specific product."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                p.id::text           AS id,
                p.party_name         AS name,
                COALESCE(
                    AVG(EXTRACT(DAY FROM
                        (COALESCE(so.received_at, so.updated_at) - so.created_at)
                    )), 7
                )::float             AS delivery_days_avg,
                COALESCE(
                    100.0 * SUM(CASE WHEN so.status = 'REJECTED' THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(so.id), 0), 0
                )::float             AS rejection_pct,
                COALESCE(
                    AVG(soi.unit_price) /
                    NULLIF((SELECT AVG(unit_price) FROM supplier_order_items), 0),
                    1.0
                )::float             AS price_index,
                COALESCE(
                    100.0 * SUM(CASE WHEN so.status IN ('CONFIRMED','RECEIVED','COMPLETED')
                                     THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(so.id), 0), 80
                )::float             AS reliability_pct,
                COUNT(DISTINCT so.id)::int AS po_count,
                COUNT(soi.id)::int         AS past_orders_for_product
            FROM parties p
            JOIN supplier_orders so ON so.supplier_id = p.id
            JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
                AND soi.product_id::text = %(pid)s
            WHERE LOWER(COALESCE(p.roletype,'')) IN ('supplier','vendor')
              AND COALESCE(p.isactive, true) = true
            GROUP BY p.id, p.party_name
            ORDER BY past_orders_for_product DESC
        """, {"pid": str(product_id)})
        return rows or []
    except Exception as e:
        log.warning(f"[SupplierIntelligence] Product metrics failed: {e}")
        return []
