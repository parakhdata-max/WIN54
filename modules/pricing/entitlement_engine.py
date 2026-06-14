"""
modules/pricing/entitlement_engine.py
======================================
Future Entitlement Engine

Handles the "earn now, redeem later" flow:
  1. create_entitlement()   — called on invoice completion
  2. get_active_entitlements() — called at punching to check if reward applies
  3. consume_entitlement()  — called when reward is applied at ₹1
  4. cancel_entitlement()   — called on credit note of source invoice
  5. expire_due_entitlements() — daily job / on-demand check

Flow:
  Qualifying invoice → create_entitlement(party_id, reward_product_id, valid_days)
  Next punching      → get_active_entitlements(party_id, product_id)
                     → if found: prompt "Reward available — apply at ₹1?"
  On accept          → consume_entitlement(entitlement_id, order_id)
  On credit note     → cancel_entitlement(entitlement_id, reason)
"""
from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger(__name__)


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _detect_schema() -> dict:
    """
    Detect actual column names in scheme_entitlements.
    Migration 0041 uses: trigger_invoice_id, trigger_order_id, valid_until
    Earlier Codex 0039 (if deployed) used: source_invoice_id, valid_to
    Auto-detects and handles both.
    """
    try:
        rows = _q("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'scheme_entitlements'
              AND table_schema = 'public'
        """) or []
        cols = {r["column_name"] for r in rows}
    except Exception:
        cols = set()

    if not cols:
        return {"inv_col": "trigger_invoice_id", "ord_col": "trigger_order_id",
                "valid_col": "valid_until", "cols": cols, "table_exists": False}

    inv_col   = "source_invoice_id" if "source_invoice_id" in cols else "trigger_invoice_id"
    ord_col   = "source_order_id"   if "source_order_id"   in cols else "trigger_order_id"
    valid_col = "valid_to"          if "valid_to"          in cols else "valid_until"

    return {"inv_col": inv_col, "ord_col": ord_col, "valid_col": valid_col,
            "cols": cols, "table_exists": True}


# Cache schema detection
_SCHEMA: dict = {}

def _schema() -> dict:
    global _SCHEMA
    if not _SCHEMA:
        _SCHEMA = _detect_schema()
    return _SCHEMA


def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as e:
        log.error(f"[entitlement] write failed: {e}")
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def create_entitlement(
    scheme_id:           str,
    party_id:            str,
    party_name:          str,
    trigger_invoice_id:  str,
    trigger_order_id:    str,
    trigger_product_id:  str,
    trigger_product_name:str,
    reward_product_id:   str,
    reward_product_name: str,
    valid_days:          int  = 30,
    reward_qty:          float = 1.0,
    reward_billing_value:float = 1.0,
    notes:               str  = "",
) -> Optional[str]:
    """
    Create a new entitlement when a qualifying product is invoiced.
    Returns entitlement ID or None on failure.
    """
    import datetime
    valid_until = (datetime.date.today() +
                   datetime.timedelta(days=valid_days)).isoformat()
    sc = _schema()
    if not sc.get("table_exists"):
        log.warning("[entitlement] scheme_entitlements table not found — run migration first")
        return None
    inv_col   = sc["inv_col"]
    ord_col   = sc["ord_col"]
    valid_col = sc["valid_col"]
    try:
        rows = _q(f"""
            INSERT INTO scheme_entitlements (
                scheme_id, party_id, party_name,
                {inv_col}, {ord_col},
                trigger_product_id, trigger_product_name,
                reward_product_id, reward_product_name,
                reward_qty, reward_billing_value,
                {valid_col}, status, notes
            ) VALUES (
                NULLIF(%(sid)s,'')::uuid,
                %(pid)s::uuid, %(pname)s,
                NULLIF(%(inv_id)s,'')::uuid,
                NULLIF(%(ord_id)s,'')::uuid,
                NULLIF(%(tpid)s,'')::uuid, %(tpname)s,
                NULLIF(%(rpid)s,'')::uuid, %(rpname)s,
                %(rqty)s, %(rbv)s,
                %(vu)s::date, 'ACTIVE', %(notes)s
            )
            RETURNING id::text
        """, {
            "sid": scheme_id, "pid": party_id, "pname": party_name,
            "inv_id": trigger_invoice_id, "ord_id": trigger_order_id,
            "tpid": trigger_product_id, "tpname": trigger_product_name,
            "rpid": reward_product_id,  "rpname": reward_product_name,
            "rqty": reward_qty, "rbv": reward_billing_value,
            "vu": valid_until, "notes": notes,
        })
        if rows:
            eid = rows[0]["id"]
            log.info(f"[entitlement] Created {eid} for party {party_name} "
                     f"reward={reward_product_name} valid_until={valid_until}")
            return eid
    except Exception as e:
        log.error(f"[entitlement] create_entitlement failed: {e}")
    return None


def get_active_entitlements(
    party_id:         str,
    reward_product_id: str = "",
) -> list:
    """
    Return active, non-expired entitlements for a party.
    Optionally filter by reward_product_id for punching-time check.
    """
    extra = ""
    params = {"pid": party_id}
    if reward_product_id:
        extra = "AND reward_product_id = %(rpid)s::uuid"
        params["rpid"] = reward_product_id

    sc = _schema()
    if not sc.get("table_exists"):
        return []
    vc = sc["valid_col"]
    return _q(f"""
        SELECT id::text, scheme_id::text,
               trigger_product_name, reward_product_id::text,
               reward_product_name, reward_qty, reward_billing_value,
               earned_at::text, {vc}::text AS valid_until,
               ({vc} - CURRENT_DATE) AS days_remaining
        FROM scheme_entitlements
        WHERE party_id = %(pid)s::uuid
          AND status = 'ACTIVE'
          AND {vc} >= CURRENT_DATE
          {extra}
        ORDER BY {vc} ASC
    """, params) or []


def consume_entitlement(
    entitlement_id: str,
    order_id:       str = "",
    invoice_id:     str = "",
) -> bool:
    """Mark entitlement as consumed when reward is applied."""
    sc = _schema()
    if not sc.get("table_exists"):
        return False
    vc = sc["valid_col"]
    return _w(f"""
        UPDATE scheme_entitlements
        SET status = 'CONSUMED',
            consumed_at = NOW(),
            consumed_order_id   = NULLIF(%(oid)s,'')::uuid,
            consumed_invoice_id = NULLIF(%(iid)s,'')::uuid
        WHERE id = %(eid)s::uuid
          AND status = 'ACTIVE'
          AND {vc} >= CURRENT_DATE
    """, {"eid": entitlement_id, "oid": order_id, "iid": invoice_id})


def cancel_entitlement(
    entitlement_id: str = "",
    trigger_invoice_id: str = "",
    reason:         str = "Source invoice reversed",
) -> bool:
    """
    Cancel entitlement — called when source invoice is credit-noted.
    Can cancel by entitlement_id OR by trigger_invoice_id.
    Returns True if cancelled, False if already consumed (needs recovery).
    """
    sc = _schema()
    if not sc.get("table_exists"):
        return True
    ic = sc["inv_col"]

    if entitlement_id:
        rows = _q("""
            SELECT status FROM scheme_entitlements
            WHERE id = %(eid)s::uuid
        """, {"eid": entitlement_id}) or []
    elif trigger_invoice_id:
        rows = _q(f"""
            SELECT id::text AS eid, status FROM scheme_entitlements
            WHERE {ic} = %(iid)s::uuid
              AND status IN ('ACTIVE','CONSUMED')
        """, {"iid": trigger_invoice_id}) or []
    else:
        return False

    if not rows:
        return True  # nothing to cancel

    already_consumed = any(r.get("status") == "CONSUMED" for r in rows)

    if entitlement_id:
        _w("""
            UPDATE scheme_entitlements
            SET status = CASE WHEN status='ACTIVE' THEN 'CANCELLED' ELSE status END,
                cancelled_at = NOW(),
                cancel_reason = %(reason)s
            WHERE id = %(eid)s::uuid
        """, {"eid": entitlement_id, "reason": reason})
    elif trigger_invoice_id:
        _w(f"""
            UPDATE scheme_entitlements
            SET status = CASE WHEN status='ACTIVE' THEN 'CANCELLED' ELSE status END,
                cancelled_at = NOW(),
                cancel_reason = %(reason)s
            WHERE {ic} = %(iid)s::uuid
        """, {"iid": trigger_invoice_id, "reason": reason})

    return not already_consumed


def expire_due_entitlements() -> int:
    """Mark past-due ACTIVE entitlements as EXPIRED. Call daily."""
    sc = _schema()
    if not sc.get("table_exists"):
        return 0
    vc = sc["valid_col"]
    _w(f"""
        UPDATE scheme_entitlements
        SET status = 'EXPIRED'
        WHERE status = 'ACTIVE'
          AND {vc} < CURRENT_DATE
    """)
    rows = _q(f"""
        SELECT COUNT(*) AS cnt FROM scheme_entitlements
        WHERE status = 'EXPIRED'
          AND cancelled_at IS NULL
          AND {vc} >= CURRENT_DATE - INTERVAL '1 day'
    """) or []
    cnt = int(rows[0].get("cnt",0)) if rows else 0
    log.info(f"[entitlement] Expired {cnt} entitlements")
    return cnt


# ── Points Ledger API ─────────────────────────────────────────────────────────

def earn_points(
    scheme_id:      str,
    party_id:       str,
    party_name:     str,
    points:         float,
    reference_type: str = "INVOICE",
    reference_id:   str = "",
    reference_no:   str = "",
    notes:          str = "",
) -> bool:
    """Post an EARN entry to points_ledger."""
    balance = get_points_balance(party_id, scheme_id)
    new_balance = round(balance + points, 2)
    return _w("""
        INSERT INTO points_ledger (
            scheme_id, party_id, party_name, entry_type,
            points, reference_type, reference_id, reference_no,
            balance_after, notes
        ) VALUES (
            NULLIF(%(sid)s,'')::uuid, %(pid)s::uuid, %(pname)s, 'EARN',
            %(pts)s, %(rtype)s, NULLIF(%(rid)s,'')::uuid, %(rno)s,
            %(bal)s, %(notes)s
        )
    """, {
        "sid": scheme_id, "pid": party_id, "pname": party_name,
        "pts": points, "rtype": reference_type,
        "rid": reference_id, "rno": reference_no,
        "bal": new_balance, "notes": notes,
    })


def get_points_balance(party_id: str, scheme_id: str = "") -> float:
    """Return current points balance for a party."""
    extra = "AND scheme_id = %(sid)s::uuid" if scheme_id else ""
    rows = _q(f"""
        SELECT COALESCE(SUM(points), 0)::float AS balance
        FROM points_ledger
        WHERE party_id = %(pid)s::uuid {extra}
    """, {"pid": party_id, "sid": scheme_id}) or []
    return float(rows[0].get("balance", 0)) if rows else 0.0


def reverse_points(
    scheme_id:    str,
    party_id:     str,
    party_name:   str,
    points:       float,
    reference_no: str = "",
    notes:        str = "Credit note reversal",
) -> bool:
    """Reverse earned points on credit note."""
    balance = get_points_balance(party_id, scheme_id)
    new_balance = round(balance - points, 2)
    return _w("""
        INSERT INTO points_ledger (
            scheme_id, party_id, party_name, entry_type,
            points, reference_type, reference_no,
            balance_after, notes
        ) VALUES (
            NULLIF(%(sid)s,'')::uuid, %(pid)s::uuid, %(pname)s, 'REVERSE',
            %(pts)s, 'CREDIT_NOTE', %(rno)s,
            %(bal)s, %(notes)s
        )
    """, {
        "sid": scheme_id, "pid": party_id, "pname": party_name,
        "pts": -abs(points), "rno": reference_no,
        "bal": new_balance, "notes": notes,
    })
