"""
fulfillment/decision_engine.py
===============================
Fulfillment Decision Engine — pure logic, zero Streamlit.

WHAT LIVES HERE
---------------
  - Route classification (STOCK / VENDOR / INHOUSE / EXTERNAL_LAB)
  - Vendor line detection
  - Assignment guard logic
  - Fulfillment mode computation (for header display)
  - Shift validation rules

WHAT DOES NOT LIVE HERE
-----------------------
  - Any st.* calls  → fulfillment/ui.py
  - DB writes        → save_order_to_db
  - Assignment panel rendering → assignment_panel.py

WHY THIS SEPARATION (Issue 3)
------------------------------
  This split means:
    - Decision logic is testable without a browser
    - Adding a rules engine later touches only this file
    - Adding an API endpoint later calls only this file
    - Streamlit can be swapped for another UI without touching decisions
"""

from typing import Dict, List, Optional, Tuple


# ── Route constants ──────────────────────────────────────────────────
ROUTE_STOCK        = "STOCK"
ROUTE_VENDOR       = "VENDOR"
ROUTE_INHOUSE      = "INHOUSE"
ROUTE_EXTERNAL_LAB = "EXTERNAL_LAB"

SUPPLIER_ROUTES = {ROUTE_VENDOR, ROUTE_EXTERNAL_LAB}
ALL_ROUTES      = {ROUTE_STOCK, ROUTE_VENDOR, ROUTE_INHOUSE, ROUTE_EXTERNAL_LAB}


# ═══════════════════════════════════════════════════════════════════════
# FULFILLMENT MODE  (drives the header banner)
# ═══════════════════════════════════════════════════════════════════════

def compute_fulfillment_mode(all_lines: List[Dict]) -> Tuple[str, str, str]:
    """
    Compute fulfillment mode label for the order header banner.

    Returns:
        (icon, label, color)
        color is one of: "info", "warning", "success"

    Examples:
        ("📦", "STOCK ONLY",  "info")
        ("🔀", "MIXED — ...", "warning")
    """
    routes = {
        line.get("manufacturing_route")
        for line in all_lines
        if line.get("manufacturing_route")
    }

    if not routes:
        return ("❓", "NOT ASSIGNED", "warning")

    if routes == {ROUTE_STOCK}:
        return ("📦", "STOCK ONLY", "info")
    if routes == {ROUTE_INHOUSE}:
        return ("🔧", "IN-HOUSE ONLY", "info")
    if routes <= SUPPLIER_ROUTES:
        return ("🏭", "SUPPLIER", "info")
    if len(routes) == 1:
        return ("📋", next(iter(routes)), "info")

    label = "MIXED — " + " + ".join(sorted(routes))
    return ("🔀", label, "warning")


def compute_route_counts(all_lines: List[Dict]) -> Dict[str, int]:
    """
    Returns count of lines per manufacturing route.
    Only includes routes with count > 0.
    """
    counts: Dict[str, int] = {}
    for line in all_lines:
        route = line.get("manufacturing_route", "UNKNOWN")
        counts[route] = counts.get(route, 0) + 1
    return {k: v for k, v in counts.items() if v > 0}


# ═══════════════════════════════════════════════════════════════════════
# VENDOR LINE DETECTION
# ═══════════════════════════════════════════════════════════════════════

def get_vendor_lines(all_lines: List[Dict]) -> List[Dict]:
    """
    Lines routed to a direct VENDOR only (Supplier PO / CLIENT_ORDER).
    EXTERNAL_LAB lines are NOT included — they go through the Lab PO flow,
    preventing auto-creation of Supplier POs for lab-assigned lines.
    """
    return [
        line for line in all_lines
        if line.get("manufacturing_route") == ROUTE_VENDOR
        and not line.get("supplier_order_id")
    ]


def get_external_lab_lines(all_lines: List[Dict]) -> List[Dict]:
    """Lines routed to an external lab — separate PO flow from direct vendor."""
    return [
        line for line in all_lines
        if line.get("manufacturing_route") == ROUTE_EXTERNAL_LAB
        and not line.get("supplier_order_id")
    ]


def get_pending_qty(line: Dict) -> int:
    """Qty not yet allocated for a line."""
    billing  = int(line.get("billing_qty", 0))
    allocated = int(line.get("allocated_qty", 0))
    return max(0, billing - allocated)


# ═══════════════════════════════════════════════════════════════════════
# ASSIGNMENT GUARD  (pure logic — no st.warning calls here)
# ═══════════════════════════════════════════════════════════════════════

def is_assignment_confirmed(session_state, all_lines=None) -> bool:
    """
    Returns True if assignments are locked in session state.
    Also returns True automatically when every non-service line is already
    STOCK-routed with allocated_qty >= billing_qty — COUNTER_SALE orders
    have stock verified at cart build time, so no manual gate is needed.

    Args:
        session_state : st.session_state
        all_lines     : Optional list of line dicts from ctx.all_lines.
                        When provided, enables the all-STOCK auto-confirm path.
    Pure check — no UI side effects.
    """
    if bool(session_state.get("bo_assignments_locked", False)):
        return True

    # Auto-confirm: all-STOCK, fully-allocated order (e.g. COUNTER_SALE)
    if all_lines:
        non_svc = [
            l for l in all_lines
            if not l.get("is_service_line")
            and str(l.get("eye_side", "")).upper() not in ("S", "SERVICE")
        ]
        if non_svc and all(
            str(l.get("manufacturing_route") or "").upper() == "STOCK"
            and int(l.get("allocated_qty") or 0) >= int(
                l.get("billing_qty") or l.get("quantity") or 0
            )
            for l in non_svc
        ):
            return True

    return False


def should_block_save(ctx) -> Tuple[bool, Optional[str]]:
    """
    Returns (should_block, reason_message).
    Called by the save button before attempting DB write.

    Checks:
      1. Billing total > 0
      2. Assignments confirmed (if panel available)
    """
    all_lines     = ctx.all_lines
    total_billing = sum(l.get("billing_total", 0) for l in all_lines)

    if total_billing <= 0:
        return (
            True,
            "Billing total is zero or negative. Check line items and pricing."
        )

    if not is_assignment_confirmed(ctx.session):
        return (
            True,
            "Supplier / Job assignments not confirmed. "
            "Scroll up and click Confirm All Assignments before saving."
        )

    return (False, None)


# ═══════════════════════════════════════════════════════════════════════
# DIAGNOSTICS  (for the no-vendor-lines case)
# ═══════════════════════════════════════════════════════════════════════

def build_route_diagnostics(all_lines: List[Dict]) -> Dict:
    """
    Build a diagnostics summary for the supplier section.
    Used when no vendor lines are found — helps operator understand why.

    Returns:
        {
            "total_lines": int,
            "routes": {route: count},
            "already_ordered": int,
            "all_vendor_ordered": bool,
            "no_vendor_routes": bool,
        }
    """
    routes: Dict[str, int] = {}
    already_ordered = 0

    for line in all_lines:
        route = line.get("manufacturing_route", "NOT_SET")
        routes[route] = routes.get(route, 0) + 1
        if line.get("supplier_order_id"):
            already_ordered += 1

    vendor_total = routes.get(ROUTE_VENDOR, 0) + routes.get(ROUTE_EXTERNAL_LAB, 0)

    return {
        "total_lines":       len(all_lines),
        "routes":            routes,
        "already_ordered":   already_ordered,
        "all_vendor_ordered": already_ordered >= vendor_total > 0,
        "no_vendor_routes":  ROUTE_VENDOR not in routes and ROUTE_EXTERNAL_LAB not in routes,
    }
