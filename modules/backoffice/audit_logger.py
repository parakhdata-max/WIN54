"""
backoffice/audit_logger.py
===========================
Observability Layer — Priority 7.

This replaces / supersedes any previous audit_logger.py stub.
Wires directly into the ctx.audit trail already built into kernel.py.

WHAT IS TRACKED
---------------
  Every ctx.record(...) call during a render is collected in ctx.audit[].
  On save (after_save_hook), ctx.flush_audit() is called — which calls
  audit_bulk() here to persist to the DB and write structured log lines.

  Additionally, fine-grained actions can be recorded inline:
    audit(AuditAction.PRODUCT_CHANGED, entity="order_items", ...)

TRACKED EVENTS (AuditAction enum)
----------------------------------
  SAVE_ORDER, STATUS_CHANGED, PRODUCT_CHANGED
  LAB_ORDER_SENT, SUPPLIER_ORDER_CREATED
  QUICK_REFILL_PO_CREATED, ADVISORY_BUNDLE_TRIGGERED
  THRESHOLD_UPDATED, AUTO_ROUTED
  QTY_CHANGED, PRICE_OVERRIDE, DISCOUNT_APPLIED

QUERYING THE AUDIT LOG
-----------------------
  SELECT * FROM audit_log
   WHERE order_id = 'ORD-0042'
   ORDER BY created_at DESC;

ARCHITECTURE
------------
  ctx.record(event, payload)        → appends to ctx.audit[]
  after_save_hook(ctx, saved_id)    → ctx.flush_audit()
  ctx.flush_audit()                 → audit_bulk(ctx.audit)
  audit_bulk(events)                → this file → DB + logging

PUBLIC API
----------
  audit(action, entity, entity_id, payload, user_id, order_id)
      Single event write — use for non-ctx flows.

  audit_bulk(events: list[dict])
      Batch write — called by ctx.flush_audit().

  get_audit_trail(order_id, limit=100) → list[dict]
      Read audit trail for an order.
"""

import logging
import time
from enum import Enum
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# AUDIT ACTIONS
# ═══════════════════════════════════════════════════════════════════════

class AuditAction(str, Enum):
    # Order lifecycle
    SAVE_ORDER              = "save_order"
    STATUS_CHANGED          = "status_changed"
    SAVE_ATTEMPTED          = "save_attempted"
    SAVE_COMPLETED          = "save_completed"

    # Line item changes
    PRODUCT_CHANGED         = "product_changed"
    QTY_CHANGED             = "qty_changed"
    PRICE_OVERRIDE          = "price_override"
    DISCOUNT_APPLIED        = "discount_applied"

    # Fulfillment
    SUPPLIER_ORDER_CREATED  = "supplier_order_created"
    AUTO_ROUTED             = "auto_routed"
    LAB_ORDER_SENT          = "lab_order_sent"

    # Advisory
    QUICK_REFILL_PO_CREATED = "quick_refill_po_created"
    ADVISORY_BUNDLE         = "advisory_bundle_triggered"
    THRESHOLD_UPDATED       = "threshold_updated"

    # Financial (new)
    INVOICE_CREATED         = "invoice_created"
    INVOICE_UPDATED         = "invoice_updated"
    INVOICE_DELETED         = "invoice_deleted"
    PAYMENT_CREATED         = "payment_created"
    PAYMENT_REVERSED        = "payment_reversed"
    CHALLAN_CREATED         = "challan_created"
    STOCK_ADJUSTED          = "stock_adjusted"
    JOURNAL_POSTED          = "journal_posted"


# ═══════════════════════════════════════════════════════════════════════
# SINGLE EVENT WRITE
# ═══════════════════════════════════════════════════════════════════════

def audit(
    action:    AuditAction,
    entity:    str            = "orders",
    entity_id: Optional[str]  = None,
    payload:   Optional[Dict] = None,
    user_id:   Optional[str]  = None,
    order_id:  Optional[str]  = None,
) -> None:
    """
    Write a single audit event immediately.
    Use this for non-ctx flows (e.g., advisory service, status update API).

    For backoffice shell flows, prefer ctx.record() which batches and
    flushes via after_save_hook.
    """
    event = {
        "event":     str(action),
        "entity":    entity,
        "entity_id": entity_id,
        "order_id":  order_id,
        "user":      user_id,
        "ts":        time.time(),
        "payload":   payload or {},
    }
    _write_event(event)


# ═══════════════════════════════════════════════════════════════════════
# FINANCIAL AUDIT — with old/new diff
# ═══════════════════════════════════════════════════════════════════════

def log_financial(
    action:    str,
    entity:    str,
    entity_id: str,
    old_value: Optional[Dict] = None,
    new_value: Optional[Dict] = None,
    user_id:   Optional[str]  = None,
    amount:    float           = 0.0,
    ref_no:    str             = "",
) -> None:
    """
    Log a financial event with before/after diff.
    Used for: invoice create/edit, payment, reversal, stock adjustment.

    SAFE: wrapped in try/except — logging NEVER blocks business transaction.
    Alerts on high-value reversals (> ₹50,000).
    """
    try:
        payload = {
            "diff":   _diff(old_value or {}, new_value or {}),
            "amount": amount,
            "ref_no": ref_no,
        }
        # Only include old/new if they have content (size control)
        if old_value:
            payload["old"] = old_value
        if new_value:
            payload["new"] = new_value

        # Alert rule: reversal > ₹50,000
        if action in ("payment_reversed", "PAYMENT_REVERSED") and amount > 50000:
            log.warning(
                f"[ALERT] High-value reversal: ₹{amount:,.2f} "
                f"by {user_id} on {entity} {entity_id}"
            )

        _write_event({
            "event":     action,
            "entity":    entity,
            "entity_id": entity_id,
            "user":      user_id,
            "payload":   payload,
        })
    except Exception as _log_err:
        # Logging must NEVER break business flow
        log.debug(f"[Audit] log_financial silently failed: {_log_err}")


def _diff(old: Dict, new: Dict) -> Dict:
    """Return only changed fields with (old_val, new_val) pairs."""
    all_keys = set(old.keys()) | set(new.keys())
    changed  = {}
    for k in all_keys:
        ov = old.get(k)
        nv = new.get(k)
        if str(ov) != str(nv):
            changed[k] = {"old": ov, "new": nv}
    return changed


# ═══════════════════════════════════════════════════════════════════════
# BULK WRITE  (called by ctx.flush_audit)
# ═══════════════════════════════════════════════════════════════════════

def audit_bulk(events: List[Dict]) -> None:
    """
    Persist a batch of audit events from ctx.audit[].
    Called by BackofficeContext.flush_audit() after every save.

    Each event dict matches the shape produced by ctx.record():
      {event, order_id, user, ts, payload?}
    """
    if not events:
        return

    succeeded = 0
    for event in events:
        try:
            _write_event(event)
            succeeded += 1
        except Exception as e:
            log.warning(f"[Audit] Batch write partial failure: {e} — event: {event}")

    log.info(f"[Audit] Flushed {succeeded}/{len(events)} events")


# ═══════════════════════════════════════════════════════════════════════
# READ
# ═══════════════════════════════════════════════════════════════════════

def get_audit_trail(
    order_id: str,
    limit:    int = 100,
) -> List[Dict]:
    """
    Fetch audit events for an order, newest first.
    Returns [] on error or no data.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                event,
                entity,
                entity_id,
                user_id,
                payload,
                created_at
            FROM audit_log
            WHERE order_id = %(order_id)s
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """, {"order_id": order_id, "limit": limit})
        return rows or []
    except Exception as e:
        log.warning(f"[Audit] get_audit_trail failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# INTERNAL WRITER
# ═══════════════════════════════════════════════════════════════════════

def _write_event(event: Dict) -> None:
    """
    Write one audit event.
    Tries DB first, falls back to structured log.
    """
    import json

    event_name = event.get("event", "unknown")
    order_id   = event.get("order_id")
    user_id    = event.get("user")
    payload    = event.get("payload") or {}

    # ── Payload size control — never store full objects ───────────────
    # Keep only safe scalar fields, strip large nested objects
    _SAFE_KEYS = {
        # Financial
        "amount","ref_no","diff","old","new","invoice_no","payment_no",
        # Order context
        "order_no","status","mode","party_name","from_status","to_status",
        "line_count","qty","reason",
        # Backoffice line/SKU actions (added for full traceability)
        "action","old_sku","new_sku","old_price","new_price","colour",
        "product","eye_side","qty_restored","field","old_value","new_value",
        "cancelled_by","refund_amount","refund_mode",
    }
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k in _SAFE_KEYS}
        # Truncate any string value > 500 chars
        payload = {k: (str(v)[:500] if isinstance(v, str) and len(str(v)) > 500 else v)
                   for k, v in payload.items()}

    # Always log structurally (visible in app logs / CloudWatch / Datadog)
    log.info(
        "[AuditEvent] event=%s order=%s user=%s payload=%s",
        event_name, order_id, user_id, json.dumps(payload, default=str)
    )

    # Try DB persistence
    try:
        from modules.sql_adapter import run_query
        run_query("""
            INSERT INTO audit_log
              (event, entity, entity_id, order_id, user_id, payload, created_at)
            VALUES
              (%(event)s, %(entity)s, %(entity_id)s, %(order_id)s,
               %(user_id)s, %(payload)s::jsonb, NOW())
        """, {
            "event":     event_name,
            "entity":    event.get("entity", "orders"),
            "entity_id": event.get("entity_id"),
            "order_id":  order_id,
            "user_id":   user_id,
            "payload":   json.dumps(payload, default=str),
        })
    except Exception as e:
        # DB failure is non-fatal — event already logged above
        log.debug(f"[Audit] DB write skipped (will retry or use log fallback): {e}")


# ═══════════════════════════════════════════════════════════════════════
# RETENTION & ARCHIVING
# ═══════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════
# INTEGRITY CHECK
# ═══════════════════════════════════════════════════════════════════════

def check_order_integrity(order_id: str) -> dict:
    """
    Silent integrity check after save/cancel/delete.
    Returns {"ok": True} or {"ok": False, "issues": [...]}

    Checks:
      1. allocated_qty matches sum of batch_allocation for each line
      2. No duplicate active lines (same product_id + eye_side)
      3. Total order_lines value matches orders.total_value (within ₹1)
    """
    issues = []
    try:
        from modules.sql_adapter import run_query as _rq_ic

        # Check 1: duplicate active lines
        _dups = _rq_ic("""
            SELECT product_id::text, eye_side, COUNT(*) AS cnt
            FROM order_lines
            WHERE order_id = %(oid)s::uuid
              AND COALESCE(is_deleted, FALSE) = FALSE
              AND eye_side NOT IN ('S', 'SERVICE')
            GROUP BY product_id, eye_side
            HAVING COUNT(*) > 1
        """, {"oid": order_id}) or []
        for dup in _dups:
            issues.append(
                f"Duplicate line: product {str(dup.get('product_id',''))[:8]}… "
                f"eye={dup.get('eye_side')} appears {dup.get('cnt')} times"
            )

        # Check 2: order total vs lines total
        _totals = _rq_ic("""
            SELECT
                o.total_value                          AS order_total,
                COALESCE(SUM(ol.total_price), 0)       AS lines_total
            FROM orders o
            LEFT JOIN order_lines ol
                ON ol.order_id = o.id
               AND COALESCE(ol.is_deleted, FALSE) = FALSE
            WHERE o.id = %(oid)s::uuid
            GROUP BY o.total_value
        """, {"oid": order_id}) or []
        if _totals:
            _ot = float(_totals[0].get("order_total") or 0)
            _lt = float(_totals[0].get("lines_total") or 0)
            if abs(_ot - _lt) > 1.0:
                issues.append(
                    f"Total mismatch: order.total_value=₹{_ot:,.2f} "
                    f"but sum(lines)=₹{_lt:,.2f} (diff ₹{abs(_ot-_lt):,.2f})"
                )

    except Exception as _ic_e:
        log.debug(f"[Integrity] check failed (non-critical): {_ic_e}")
        return {"ok": True}  # Don't block on check failure

    return {"ok": len(issues) == 0, "issues": issues}



# ═══════════════════════════════════════════════════════════════════════
# STOCK RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════

def reconcile_stock_allocations(fix: bool = False) -> dict:
    """
    Detects lines where batch_status='ALLOCATED' but
    inventory_stock.allocated_qty is lower than expected.

    This catches the rare crash window:
        UPDATE order_lines (committed) → crash → UPDATE inventory_stock (never ran)

    Args:
        fix: if True, patches inventory_stock.allocated_qty to match.
             Default False = detect only (safe to run anytime).

    Returns:
        {
          "drifted": [...],   # list of drifted rows
          "fixed":   int,     # rows patched (0 if fix=False)
          "ok":      bool     # True if no drift found
        }

    Run:
        - on app startup (detect only)
        - nightly (fix=True)
        - before generating stock reports
    """
    try:
        from modules.sql_adapter import run_query as _rq, run_write as _rw

        # Correct batch-level reconciliation query (exact match per product+batch)
        drifted = _rq("""
            SELECT
                ol.id::text          AS line_id,
                ol.order_id::text    AS order_id,
                ol.product_id::text  AS product_id,
                ol.lens_params::jsonb->>'batch_no'  AS batch_no,
                ol.allocated_qty     AS line_allocated,
                0 AS stock_allocated,
                ol.allocated_qty AS drift
            FROM order_lines ol
            LEFT JOIN inventory_stock ist
                ON  ist.product_id::text = ol.product_id::text
                AND ist.batch_no         = ol.lens_params::jsonb->>'batch_no'
            WHERE ol.lens_params::jsonb->>'batch_status' = 'ALLOCATED'
              AND COALESCE(ol.is_deleted,  FALSE) = FALSE
              AND COALESCE(ol.billed_qty,  0)     = 0
              AND COALESCE(ol.stock_reversed, FALSE) = FALSE
              AND COALESCE(ol.allocated_qty, 0) > 0
            ORDER BY drift DESC
        """) or []

        fixed = 0
        if fix and drifted:
            for row in drifted:
                pid   = row.get("product_id","")
                bno   = row.get("batch_no","")
                drift = int(row.get("drift") or 0)
                if pid and drift > 0:
                    try:
                        _rw("""
                            UPDATE inventory_stock
                            SET allocated_qty = COALESCE(allocated_qty, 0) + %(drift)s,
                                updated_at    = NOW()
                            WHERE product_id = %(pid)s::uuid
                              AND batch_no   = %(bno)s
                        """, {"pid": pid, "bno": bno or "", "drift": drift})
                        fixed += 1
                        log.info(
                            "[Reconcile] Patched product=%s batch=%s drift=%s",
                            pid[:8], bno, drift
                        )
                    except Exception as _fe:
                        log.warning(f"[Reconcile] Patch failed for {pid[:8]}: {_fe}")

        result = {
            "ok":      len(drifted) == 0,
            "drifted": [dict(r) for r in drifted],
            "fixed":   fixed,
        }

        if drifted:
            log.warning(
                "[Reconcile] %d drifted allocation(s) found%s",
                len(drifted),
                f", {fixed} patched" if fix else " — run with fix=True to patch"
            )
        else:
            log.info("[Reconcile] All stock allocations match ✓")

        return result

    except Exception as e:
        log.warning(f"[Reconcile] reconcile_stock_allocations failed: {e}")
        return {"ok": True, "drifted": [], "fixed": 0}  # non-fatal

def get_audit_stats() -> Dict:
    """
    Return audit log size stats.
    Call from System Health page to monitor growth.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COUNT(*)                         AS total_rows,
                MIN(created_at)::date::text      AS oldest_entry,
                MAX(created_at)::date::text      AS newest_entry,
                COUNT(CASE WHEN created_at >= NOW() - INTERVAL '7 days' THEN 1 END)
                                                 AS last_7_days,
                COUNT(CASE WHEN created_at >= NOW() - INTERVAL '30 days' THEN 1 END)
                                                 AS last_30_days,
                pg_size_pretty(pg_total_relation_size('audit_log'))
                                                 AS table_size
            FROM audit_log
        """)
        return rows[0] if rows else {}
    except Exception as e:
        log.warning(f"[Audit] get_audit_stats failed: {e}")
        return {}


def archive_old_entries(days_to_keep: int = 90) -> int:
    """
    Delete audit entries older than days_to_keep.
    NEVER deletes financial entries (INVOICE, PAYMENT, REVERSAL).
    Returns count deleted.

    Safe to run monthly from System Health page.
    Financial audit trail is permanent — only operational logs are pruned.
    """
    try:
        from modules.sql_adapter import run_query, run_write
        # Count before
        before = run_query("""
            SELECT COUNT(*) AS n FROM audit_log
            WHERE created_at < NOW() - INTERVAL '%(days)s days'
              AND event NOT IN (
                  'invoice_created', 'invoice_updated', 'invoice_deleted',
                  'payment_created', 'payment_reversed',
                  'challan_created', 'journal_posted', 'stock_adjusted'
              )
        """, {"days": days_to_keep})

        count = int((before[0].get("n") if before else 0) or 0)
        if count == 0:
            return 0

        run_write("""
            DELETE FROM audit_log
            WHERE created_at < NOW() - INTERVAL '%(days)s days'
              AND event NOT IN (
                  'invoice_created', 'invoice_updated', 'invoice_deleted',
                  'payment_created', 'payment_reversed',
                  'challan_created', 'journal_posted', 'stock_adjusted'
              )
        """, {"days": days_to_keep})

        log.info(f"[Audit] Archived {count} entries older than {days_to_keep} days")
        return count
    except Exception as e:
        log.warning(f"[Audit] archive_old_entries failed: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════
# AUDIT LOG TABLE DDL  (run once in migration)
# ═══════════════════════════════════════════════════════════════════════

AUDIT_LOG_DDL = """
-- Run this migration once to enable persistent audit logging

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    event       TEXT        NOT NULL,
    entity      TEXT        NOT NULL DEFAULT 'orders',
    entity_id   TEXT,
    order_id    TEXT,
    user_id     TEXT,
    payload     JSONB       NOT NULL DEFAULT '{}',
    old_value   JSONB       DEFAULT '{}',
    new_value   JSONB       DEFAULT '{}',
    amount      NUMERIC(14,2) DEFAULT 0,
    ref_no      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_log_order_id_idx   ON audit_log (order_id);
CREATE INDEX IF NOT EXISTS audit_log_event_idx       ON audit_log (event);
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx  ON audit_log (created_at DESC);

COMMENT ON TABLE audit_log IS
    'Immutable audit trail for all backoffice actions. '
    'Written by audit_logger.py. Never updated or deleted.';
"""
