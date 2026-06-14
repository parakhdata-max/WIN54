"""
order_status_live.py
─────────────────────────────────────────────────────────────────────────────
Single source of truth for order status display across ALL pages.

Usage (any module):
    from modules.backoffice.order_status_live import get_live_status, STATUS_META

    status = get_live_status(order)          # e.g. "UNDER_REVIEW"
    meta   = STATUS_META[status]
    print(meta["label"], meta["icon"], meta["color"])

Rules (in priority order):
  1. CANCELLED / DELIVERED / CLOSED     → trust DB, terminal
  2. Has challan or invoice              → BILLED
  3. BILLED in DB but no docs           → downgrade to READY
  4. CONFIRMED but no history entry     → UNDER_REVIEW (auto-confirmed by old code)
  5. CONFIRMED with history entry       → CONFIRMED
  6. Has active job_master rows         → IN_PRODUCTION (if stage not done)
  7. UNDER_REVIEW / PENDING / rest      → as-is
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
from typing import Dict, Optional

# ── Canonical alias map — legacy/transient → canonical ───────────────────────
_ALIAS: Dict[str, str] = {
    "PENDING_VALIDATION":  "PENDING",
    "PROVISIONAL":         "PENDING",
    "ORDER_SAVED":         "PENDING",
    "READY_FOR_BILLING":   "READY_FOR_BILLING",  # own station
    "UNDER_REVIEW":        "UNDER_REVIEW",
    "PENDING_PAYMENT":     "PENDING_PAYMENT",    # own station — payment gate
    "HOLD":                "HOLD",
    "ON_HOLD":             "HOLD",
    "CREDIT_HOLD":         "CREDIT_HOLD",
}

# ── Done job stages — means job is finished, order can move to READY ─────────
_JOB_DONE_STAGES = {
    # Inhouse final stages — job complete, order moves to READY/BILLING
    "READY_FOR_PACK",       # packing step — visible in pipeline
    "READY_TO_BILL",        # billing gate open (is_closed=TRUE)
    "READY_FOR_BILLING",    # supplier pipeline terminal (alias)
    "FINAL_QC",
    "FITTING_DONE",
    "DELIVERED",
    "INVOICED",
    # Coating completion stages
    "HARDCOAT_COMPLETED",
    "HARDCOAT_DONE",        # canonical stage name
    "ARC_RECEIVED",
    "COLOURING_COMPLETED",
    "COLOURING_DONE",
    "PRODUCTION_COMPLETED",
    "PRODUCTION_DONE",
    "DISPATCHED",
}

# ── Status metadata — one place to update labels/icons/colors ────────────────
STATUS_META: Dict[str, Dict] = {
    "PENDING":          {"label": "Received",        "icon": "📥", "color": "#3b82f6", "badge": "blue"},
    "UNDER_REVIEW":     {"label": "Under Review",    "icon": "🔍", "color": "#f59e0b", "badge": "amber"},
    "CONFIRMED":        {"label": "Confirmed",       "icon": "✅", "color": "#6366f1", "badge": "indigo"},
    "IN_PRODUCTION":    {"label": "In Production",   "icon": "⚙️", "color": "#8b5cf6", "badge": "purple"},
    "READY":            {"label": "Ready",           "icon": "📦", "color": "#10b981", "badge": "green"},
    "READY_FOR_BILLING":{"label": "To Bill",         "icon": "🚀", "color": "#0d9488", "badge": "teal"},
    "PARTIALLY_BILLED": {"label": "Part Billed",     "icon": "⚡", "color": "#f59e0b", "badge": "amber"},
    "CHALLANED":        {"label": "Challaned",       "icon": "📋", "color": "#3b82f6", "badge": "blue"},
    "BILLED":           {"label": "Invoiced",         "icon": "🧾", "color": "#059669", "badge": "emerald"},
    "DISPATCHED":       {"label": "Dispatched",      "icon": "🚚", "color": "#0891b2", "badge": "cyan"},
    "DELIVERED":        {"label": "Delivered",       "icon": "✅", "color": "#10b981", "badge": "green"},
    "CLOSED":           {"label": "Closed",          "icon": "🔒", "color": "#334155", "badge": "slate"},
    "PENDING_PAYMENT":  {"label": "Awaiting Payment","icon": "💳", "color": "#f97316", "badge": "orange"},
    "HOLD":             {"label": "On Hold",         "icon": "⏸️", "color": "#f97316", "badge": "orange"},
    "CREDIT_HOLD":      {"label": "Credit Hold",     "icon": "🛑", "color": "#dc2626", "badge": "red"},
    "CANCELLED":        {"label": "Cancelled",       "icon": "❌", "color": "#ef4444", "badge": "red"},
}

# ── Train stations in order ───────────────────────────────────────────────────
STATUS_TRAIN = [
    "PENDING", "UNDER_REVIEW", "HOLD", "CREDIT_HOLD", "PENDING_PAYMENT", "CONFIRMED",
    "IN_PRODUCTION", "READY", "READY_FOR_BILLING",
    "PARTIALLY_BILLED", "CHALLANED", "BILLED",
    "DISPATCHED", "DELIVERED", "CLOSED",
]


def _rq(sql: str, params: dict):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def get_live_status(order: dict) -> str:
    """
    Canonical live status for an order. Use this everywhere.
    Checks DB state on every call — results are fast (indexed queries).
    """
    raw    = str(order.get("status") or "PENDING").upper()
    status = _ALIAS.get(raw, raw)

    # ── Terminal — trust DB ───────────────────────────────────────────────
    # NOTE: PARTIALLY_BILLED is intentionally NOT terminal — it must fall
    # through to the billing check below so it can be promoted to BILLED
    # once all lines are covered by challans/invoices.
    if status in ("CANCELLED", "DELIVERED", "CLOSED", "DISPATCHED"):
        return status

    oid = str(order.get("id") or "")
    ono = str(order.get("order_no") or "")

    # ── Billing check — compute from challan line coverage ───────────────
    # Source of truth: how many product lines have active challan lines?
    # ALL covered   → BILLED
    # SOME covered  → PARTIALLY_BILLED
    # NONE covered  → no change
    if oid or ono:
        try:
            _oid_txt = oid if oid else "__none__"
            _ono_txt = ono if ono else "__none__"

            coverage = _rq("""
                SELECT
                    COUNT(DISTINCT ol.id) AS total_lines,
                    COUNT(DISTINCT cl.order_line_id) AS billed_lines,
                    COUNT(DISTINCT i.id) AS invoiced_challans
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                LEFT JOIN challan_lines cl ON cl.order_line_id = ol.id
                    AND EXISTS (
                        SELECT 1 FROM challans c
                        WHERE c.id = cl.challan_id
                          AND c.status NOT IN ('CANCELLED','VOID','DELETED')
                    )
                LEFT JOIN challans ch2 ON ch2.order_ids::text[] @> ARRAY[o.id::text]
                    AND ch2.status NOT IN ('CANCELLED','VOID','DELETED')
                LEFT JOIN invoices i ON i.challan_id = ch2.id
                    AND i.status NOT IN ('CANCELLED','VOID')
                WHERE (o.id::text = %(oid)s OR o.order_no = %(ono)s)
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND COALESCE(ol.is_service_line, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.eye_side, '')) NOT IN ('S','SERVICE')
            """, {"oid": _oid_txt, "ono": _ono_txt})

            if coverage:
                _total    = int(coverage[0].get("total_lines") or 0)
                _billed   = int(coverage[0].get("billed_lines") or 0)
                _invoiced = int(coverage[0].get("invoiced_challans") or 0)

                if _total > 0:
                    # Normal order: check product line coverage
                    if _billed >= _total:
                        if _invoiced > 0:
                            return "BILLED"
                        else:
                            return "CHALLANED"
                    elif _billed > 0:
                        return "PARTIALLY_BILLED"
                else:
                    # Service-only order (total_lines=0) — check challan/invoice existence
                    svc_cov = _rq("""
                        SELECT
                            COUNT(DISTINCT cl.id) AS challan_lines,
                            COUNT(DISTINCT i.id)  AS invoices
                        FROM order_lines ol
                        JOIN orders o ON o.id = ol.order_id
                        LEFT JOIN challan_lines cl ON cl.order_line_id = ol.id
                            AND EXISTS (
                                SELECT 1 FROM challans c
                                WHERE c.id = cl.challan_id
                                  AND c.status NOT IN ('CANCELLED','VOID','DELETED')
                            )
                        LEFT JOIN challans ch2 ON ch2.order_ids::text[] @> ARRAY[o.id::text]
                            AND ch2.status NOT IN ('CANCELLED','VOID','DELETED')
                        LEFT JOIN invoices i ON i.challan_id = ch2.id
                            AND i.status NOT IN ('CANCELLED','VOID')
                        WHERE (o.id::text = %(oid)s OR o.order_no = %(ono)s)
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                          AND COALESCE(ol.is_service_line, FALSE) = TRUE
                    """, {"oid": _oid_txt, "ono": _ono_txt})
                    if svc_cov and int(svc_cov[0].get("challan_lines") or 0) > 0:
                        if int(svc_cov[0].get("invoices") or 0) > 0:
                            return "BILLED"
                        return "CHALLANED"
        except Exception:
            pass

    # READY_FOR_BILLING with no docs yet — return as-is
    if status == "READY_FOR_BILLING":
        return status

    # Downgrade stale BILLED
    if status == "BILLED":
        return "READY"

    # ── CONFIRMED — check if actually human-confirmed in backoffice ─────
    # Auto-saves (Retail Desk, system, Administrator etc.) don't count.
    # Only a real named operator clicking Confirm in backoffice counts.
    if status == "CONFIRMED":
        try:
            # _SYSTEM_NAMES: auto-saves that should NOT count as human confirmation.
            # Punching desks (retail_desk, wholesale_desk) ARE human operators —
            # they are NOT in this set. Only true automation is listed here.
            _SYSTEM_NAMES = {
                "", "system", "production_engine", "system_auto",
                "auto", "none", "system_save",
            }
            _oid_val = oid if (oid and len(oid) == 36) else None
            hist = _rq("""
                SELECT h.changed_by_name FROM order_status_history h
                JOIN orders o ON o.id = h.order_id
                WHERE (%(oid)s IS NOT NULL AND o.id = %(oid)s::uuid
                    OR o.order_no = %(ono)s)
                  AND h.to_status = 'CONFIRMED'
            """, {"oid": _oid_val, "ono": ono})
            _human_confirmed = any(
                str(r.get("changed_by_name") or "").lower().strip()
                not in _SYSTEM_NAMES
                for r in (hist or [])
            )
            if not _human_confirmed:
                status = "UNDER_REVIEW"
        except Exception:
            pass  # keep CONFIRMED if query fails

    # ── Supplier/Lab lines — check ready_qty vs quantity ─────────────────
    # VENDOR and EXTERNAL_LAB lines are "production" for our pipeline.
    # They are ready when supplier has delivered (ready_qty >= quantity).
    if status in ("CONFIRMED","UNDER_REVIEW","IN_PRODUCTION","READY","READY_FOR_BILLING"):
        try:
            _oid_sup = oid if (oid and len(oid) == 36) else None
            sup_lines = _rq("""
                SELECT
                    ol.quantity,
                    COALESCE(ol.ready_qty, 0)  AS ready_qty,
                    COALESCE(ol.billed_qty, 0) AS billed_qty,
                    ol.lens_params
                FROM order_lines ol
                JOIN orders o ON o.id = ol.order_id
                WHERE (%(oid)s IS NOT NULL AND o.id = %(oid)s::uuid
                    OR o.order_no = %(ono)s)
                  AND (
                      ol.lens_params->>'manufacturing_route' IN ('VENDOR','EXTERNAL_LAB')
                      OR ol.lens_params->>'job_type' IN ('VENDOR','EXTERNAL_LAB')
                  )
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND COALESCE(ol.billed_qty, 0) = 0
            """, {"oid": _oid_sup, "ono": ono})
            if sup_lines:
                _all_sup_ready = all(
                    int(r.get("ready_qty") or 0) >= int(r.get("quantity") or 1)
                    for r in sup_lines
                )
                if not _all_sup_ready:
                    # Has undelivered supplier/lab lines → IN_PRODUCTION
                    if status not in ("CONFIRMED","UNDER_REVIEW","IN_PRODUCTION"):
                        pass  # don't downgrade from higher statuses
                    else:
                        return "IN_PRODUCTION"
        except Exception:
            pass

    # ── IN_PRODUCTION — check job_master (INHOUSE lines) ─────────────────
    if status in ("CONFIRMED", "UNDER_REVIEW", "PENDING_PAYMENT", "READY", "PENDING", "READY_FOR_BILLING", "IN_PRODUCTION"):
        try:
            _oid_jm = oid if (oid and len(oid) == 36) else None
            jobs = _rq("""
                SELECT jm.current_stage, jm.is_closed
                FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                JOIN orders o       ON o.id  = ol.order_id
                WHERE (%(oid)s IS NOT NULL AND o.id = %(oid)s::uuid
                    OR o.order_no = %(ono)s)
                  AND NOT jm.is_closed
                  AND COALESCE(ol.is_service_line, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE')
                  AND UPPER(COALESCE(ol.batch_status,'')) NOT IN ('CONFIRMED','DRAFT')
                LIMIT 5
            """, {"oid": _oid_jm, "ono": ono})
            if jobs:
                all_done = all(j.get("current_stage") in _JOB_DONE_STAGES for j in jobs)
                if all_done:
                    return "READY"
                return "IN_PRODUCTION"
        except Exception:
            pass

    return status


def get_status_meta(status: str) -> Dict:
    """Return label/icon/color for any status string."""
    return STATUS_META.get(status, {
        "label": status, "icon": "•", "color": "#64748b", "badge": "slate"
    })


def status_badge_html(status: str, size: str = "0.75rem") -> str:
    """Return a styled HTML badge for the status."""
    m = get_status_meta(status)
    return (
        f"<span style='background:{m['color']}22;color:{m['color']};"
        f"border:1px solid {m['color']}55;padding:2px 10px;border-radius:12px;"
        f"font-size:{size};font-weight:700;white-space:nowrap'>"
        f"{m['icon']} {m['label']}</span>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT GATE — FULL_ADVANCE parties
# ─────────────────────────────────────────────────────────────────────────────

def check_confirm_gate(order_id: str, party_id: str = None) -> dict:
    """
    Check if an order can be CONFIRMED based on the party's billing_category.

    For FULL_ADVANCE parties:
        Order can be punched and edited freely.
        CONFIRM is blocked until full payment is received.

    Returns:
        {
            "allowed":          bool,
            "billing_category": str,
            "reason":           str,     # shown to operator if blocked
            "paid":             float,
            "total":            float,
            "balance":          float,
            "suggested_status": str,     # PENDING_PAYMENT if blocked
        }
    """
    try:
        from modules.sql_adapter import run_query
    except ImportError:
        return {"allowed": True, "billing_category": "ON_COMPLETION",
                "reason": "", "paid": 0, "total": 0, "balance": 0,
                "suggested_status": "UNDER_REVIEW"}

    # Get order total and party billing category
    order_rows = run_query("""
        SELECT
            o.total_value,
            COALESCE(p.billing_category,
                     p.payment_mode,
                     'ON_COMPLETION')           AS billing_category,
            COALESCE(p.credit_limit, 0)         AS credit_limit,
            COALESCE(p.party_name, '')          AS party_name
        FROM orders o
        LEFT JOIN parties p ON p.id = o.party_id
        WHERE o.id = %(oid)s::uuid
        LIMIT 1
    """, {"oid": order_id}) or []

    if not order_rows:
        return {"allowed": True, "billing_category": "ON_COMPLETION",
                "reason": "Order not found", "paid": 0, "total": 0, "balance": 0,
                "suggested_status": "UNDER_REVIEW"}

    r            = order_rows[0]
    billing_cat  = str(r.get("billing_category") or "ON_COMPLETION").upper()
    total        = float(r.get("total_value") or 0)
    party_name   = r.get("party_name", "")

    # Only FULL_ADVANCE requires payment before confirm
    if billing_cat != "FULL_ADVANCE":
        return {"allowed": True, "billing_category": billing_cat,
                "reason": "", "paid": 0, "total": total, "balance": 0,
                "suggested_status": "CONFIRMED"}

    # Check payments received for this order
    paid_rows = run_query("""
        SELECT COALESCE(SUM(amount), 0) AS paid
        FROM payments
        WHERE advance_for_order_id = %(oid)s::uuid
          AND COALESCE(is_deleted, FALSE) = FALSE
    """, {"oid": order_id}) or []
    paid    = float(paid_rows[0]["paid"] if paid_rows else 0)
    balance = max(total - paid, 0)

    if balance <= 0.01:
        # Fully paid — allow confirm
        return {"allowed": True, "billing_category": billing_cat,
                "reason": "", "paid": paid, "total": total, "balance": 0,
                "suggested_status": "CONFIRMED"}

    # Not paid — block confirm, suggest PENDING_PAYMENT status
    return {
        "allowed":          False,
        "billing_category": billing_cat,
        "reason": (
            f"💳 FULL ADVANCE policy — {party_name} must pay in full before order is confirmed. "
            f"Received: ₹{paid:,.2f} | Total: ₹{total:,.2f} | Balance: ₹{balance:,.2f}"
        ),
        "paid":             paid,
        "total":            total,
        "balance":          balance,
        "suggested_status": "PENDING_PAYMENT",
    }


def apply_confirm_gate_to_persistence(order: dict) -> dict:
    """
    Called by order_persistence.py before setting status to CONFIRMED.
    If party is FULL_ADVANCE and balance > 0, overrides status to PENDING_PAYMENT.

    Usage in order_persistence.py:
        from modules.backoffice.order_status_live import apply_confirm_gate_to_persistence
        order = apply_confirm_gate_to_persistence(order)
        # then save order normally
    """
    if str(order.get("status") or "").upper() != "CONFIRMED":
        return order   # Only intercept CONFIRMED transitions

    order_id = str(order.get("id") or order.get("order_id") or "")
    party_id = str(order.get("party_id") or "")

    if not order_id:
        return order

    gate = check_confirm_gate(order_id, party_id)
    if not gate["allowed"]:
        order = dict(order)   # don't mutate caller's dict
        order["status"]         = gate["suggested_status"]  # PENDING_PAYMENT
        order["_gate_reason"]   = gate["reason"]
        order["_gate_balance"]  = gate["balance"]
    return order


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE + PERSIST — single call to sync order status to DB
# ─────────────────────────────────────────────────────────────────────────────

def compute_order_status(order: dict, write: bool = True) -> str:
    """
    Compute the correct live status for an order and optionally write it to DB.

    This is the ONE function all UI pages should call after any state change:
      - After marking supplier delivery (ready_qty update)
      - After advancing job card stage
      - After billing (challan/invoice created)
      - After dispatch

    Usage:
        from modules.backoffice.order_status_live import compute_order_status
        new_status = compute_order_status(order, write=True)

    Returns the computed status string.
    """
    live = get_live_status(order)

    if write:
        oid = str(order.get("id") or "")
        cur = str(order.get("status") or "").upper()
        if oid and live != cur:
            try:
                from modules.sql_adapter import run_write
                # Lock only on full BILLED; unlock if PARTIALLY_BILLED
                _lock_val = True  if live in ("BILLED","CHALLANED") else None
                _lock_val = False if live == "PARTIALLY_BILLED"  else _lock_val
                if _lock_val is not None:
                    run_write(
                        "UPDATE orders SET status=%(st)s, is_locked=%(lk)s, updated_at=NOW() WHERE id=%(oid)s::uuid",
                        {"st": live, "lk": _lock_val, "oid": oid}
                    )
                else:
                    run_write(
                        "UPDATE orders SET status = %(st)s, updated_at = NOW() WHERE id = %(oid)s::uuid",
                        {"st": live, "oid": oid}
                    )
                # Also write to order_status_history
                try:
                    import uuid as _u
                    run_write("""
                        INSERT INTO order_status_history
                            (history_id, order_id, from_status, to_status, changed_by_name, changed_at)
                        VALUES (%s::uuid, %s::uuid, %s, %s, 'system_auto', NOW())
                        ON CONFLICT DO NOTHING
                    """, (str(_u.uuid4()), oid, cur, live))
                except Exception:
                    pass  # history table optional
                try:
                    from modules.backoffice.backoffice_helpers import load_orders_from_database
                    load_orders_from_database.clear()
                except Exception:
                    pass
            except Exception:
                pass  # non-fatal — status will sync on next load

    return live


def sync_all_open_orders() -> int:
    """
    Batch-sync status for all non-terminal orders.
    Call this on startup or as a scheduled health check.
    Returns count of orders updated.
    """
    try:
        from modules.sql_adapter import run_query
        orders = run_query("""
            SELECT id::text AS id, order_no, status
            FROM orders
            WHERE status NOT IN ('CLOSED','CANCELLED','DELIVERED','DISPATCHED')
            ORDER BY updated_at DESC
            LIMIT 500
        """, {}) or []
    except Exception:
        return 0

    updated = 0
    for row in orders:
        order = {"id": row["id"], "order_no": row["order_no"], "status": row["status"]}
        new_st = compute_order_status(order, write=True)
        if new_st != str(row["status"]).upper():
            updated += 1
    return updated
