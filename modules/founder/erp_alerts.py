"""
modules/founder/erp_alerts.py
──────────────────────────────────────────────────────────────────
ERP Intelligence Alert Engine.

Runs detection queries and writes to erp_alerts table.
Call run_alert_detection() on owner dashboard load or app startup.
Results read back via get_active_alerts().

WA delivery: uses wa_hub wa.me links (no API needed).
When Meta Business API is configured, swap _send_wa_push() stub.
"""
from __future__ import annotations
import logging
from datetime import date
from typing import List, Dict

_log = logging.getLogger(__name__)


def _q(sql: str, params=None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        _log.warning(f"[erp_alerts._q] {e}")
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        _log.warning(f"[erp_alerts._w] {e}")
        return False


def _insert_alert(alert_type: str, severity: str, title: str,
                  detail: str = "", ref_id: str = "", ref_value: str = "") -> None:
    """Write alert — deduplicated by (type + ref_id + today's date)."""
    _w("""
        INSERT INTO erp_alerts
            (alert_type, severity, title, detail, ref_id, ref_value)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (alert_type, severity, title, detail, ref_id, ref_value))


# ──────────────────────────────────────────────────────────────────────────────
# DETECTION RULES
# ──────────────────────────────────────────────────────────────────────────────

def _detect_low_margin() -> int:
    rows = _q("SELECT order_no, product_name, margin_pct FROM v_alert_low_margin LIMIT 20")
    for r in rows:
        _insert_alert(
            alert_type="LOW_MARGIN",
            severity="CRITICAL" if (r.get("margin_pct") or 0) < 0 else "WARN",
            title=f"Low margin — {r['order_no']}",
            detail=f"{r['product_name']} sold at {r['margin_pct']}% margin",
            ref_id=str(r["order_no"]),
            ref_value=f"{r['margin_pct']}%",
        )
    return len(rows)


def _detect_discount_abuse() -> int:
    rows = _q("SELECT order_no, product_name, discount_by, margin_pct FROM v_alert_discount LIMIT 20")
    for r in rows:
        _insert_alert(
            alert_type="DISCOUNT_ABUSE",
            severity="WARN",
            title=f"Discount given — {r['order_no']}",
            detail=f"{r['product_name']} by {r.get('discount_by','?')} → margin {r['margin_pct']}%",
            ref_id=str(r["order_no"]),
            ref_value=f"{r['margin_pct']}%",
        )
    return len(rows)


def _detect_price_outlier() -> int:
    rows = _q("SELECT order_no, product_name, unit_price, avg_price, pct_below_avg FROM v_alert_price_outlier LIMIT 20")
    for r in rows:
        _insert_alert(
            alert_type="PRICE_OUTLIER",
            severity="WARN",
            title=f"Price outlier — {r['order_no']}",
            detail=f"{r['product_name']} sold at ₹{r['unit_price']} vs avg ₹{r['avg_price']} ({r['pct_below_avg']}% below)",
            ref_id=str(r["order_no"]),
            ref_value=f"{r['pct_below_avg']}% below avg",
        )
    return len(rows)


def _detect_cash_gap() -> int:
    rows = _q("SELECT txn_date, total_invoiced, total_collected, net_outstanding FROM v_alert_cash_gap LIMIT 5")
    for r in rows:
        gap = float(r.get("net_outstanding") or 0)
        _insert_alert(
            alert_type="CASH_GAP",
            severity="CRITICAL" if gap > 5000 else "WARN",
            title=f"Cash gap — {r['txn_date']}",
            detail=f"Invoiced ₹{float(r['total_invoiced'] or 0):,.0f} vs collected ₹{float(r['total_collected'] or 0):,.0f}",
            ref_id=str(r["txn_date"]),
            ref_value=f"₹{gap:,.0f}",
        )
    return len(rows)


def _detect_low_stock() -> int:
    rows = _q("SELECT product_name, available FROM v_reorder_alert WHERE available <= 2 LIMIT 20")
    for r in rows:
        _insert_alert(
            alert_type="STOCK_LOW",
            severity="CRITICAL" if int(r.get("available") or 0) <= 0 else "WARN",
            title=f"Low stock — {r['product_name']}",
            detail=f"Only {r['available']} units available",
            ref_id=str(r["product_name"]),
            ref_value=str(r["available"]),
        )
    return len(rows)


def _detect_po_overdue() -> int:
    rows = _q("SELECT po_no, supplier, days_pending, po_value FROM v_po_overdue LIMIT 10")
    for r in rows:
        _insert_alert(
            alert_type="PO_OVERDUE",
            severity="CRITICAL" if int(r.get("days_pending") or 0) > 21 else "WARN",
            title=f"PO overdue — {r['po_no']}",
            detail=f"{r['supplier']} — {r['days_pending']} days since PO sent, goods not received",
            ref_id=str(r["po_no"]),
            ref_value=f"{r['days_pending']} days",
        )
    return len(rows)


def _detect_tally_unsynced() -> int:
    rows = _q("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(grand_total),0) AS val
        FROM invoices
        WHERE tally_synced = FALSE AND status NOT IN ('CANCELLED','VOID')
          AND created_at < NOW() - INTERVAL '1 day'
    """)
    cnt = int((rows[0].get("cnt") or 0)) if rows else 0
    if cnt > 0:
        val = float((rows[0].get("val") or 0)) if rows else 0
        _insert_alert(
            alert_type="TALLY_UNSYNCED",
            severity="WARN",
            title=f"Tally sync pending — {cnt} invoices",
            detail=f"₹{val:,.0f} in invoices not yet synced to Tally",
            ref_id=f"tally_{date.today()}",
            ref_value=str(cnt),
        )
    return cnt


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

def run_alert_detection() -> Dict[str, int]:
    """
    Run all detection rules. Idempotent — safe to call on every dashboard load.
    Returns dict of {rule: count_flagged}.
    """
    # Clear stale read+dismissed alerts older than 24h to keep table clean
    _w("""
        DELETE FROM erp_alerts
        WHERE is_dismissed = TRUE
          AND created_at < NOW() - INTERVAL '24 hours'
    """)

    return {
        "low_margin":     _detect_low_margin(),
        "discount_abuse": _detect_discount_abuse(),
        "price_outlier":  _detect_price_outlier(),
        "cash_gap":       _detect_cash_gap(),
        "stock_low":      _detect_low_stock(),
        "po_overdue":     _detect_po_overdue(),
        "tally_unsynced": _detect_tally_unsynced(),
    }


def get_active_alerts(severity: str = None) -> List[Dict]:
    """Fetch unread, undismissed alerts — optionally filter by severity."""
    sev_filter = "AND severity = %(sev)s" if severity else ""
    return _q(f"""
        SELECT id::text, alert_type, severity, title, detail,
               ref_id, ref_value, created_at::text, notified_wa
        FROM erp_alerts
        WHERE is_dismissed = FALSE
          {sev_filter}
        ORDER BY
            CASE severity WHEN 'CRITICAL' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
            created_at DESC
        LIMIT 50
    """, {"sev": severity} if severity else {})


def dismiss_alert(alert_id: str) -> None:
    _w("UPDATE erp_alerts SET is_dismissed=TRUE WHERE id=%s::uuid", (alert_id,))


def mark_read(alert_id: str) -> None:
    _w("UPDATE erp_alerts SET is_read=TRUE WHERE id=%s::uuid", (alert_id,))


def wa_alert_link(alert: Dict, owner_mobile: str) -> str:
    """Build a wa.me link for owner to tap — sends alert detail to themselves."""
    from modules.wa_hub import wa_link
    msg = (
        f"🚨 ERP ALERT — {alert.get('severity','WARN')}\n\n"
        f"{alert.get('title','')}\n"
        f"{alert.get('detail','')}\n\n"
        f"Value: {alert.get('ref_value','')}\n"
        f"Ref: {alert.get('ref_id','')}\n\n"
        f"— WIN54 ERP"
    )
    return wa_link(owner_mobile, msg)


# ──────────────────────────────────────────────────────────────────────────────
# CRON RUNNER — call this from Windows Task Scheduler or cron every 15 min
# Usage: python -m modules.founder.erp_alerts
# ──────────────────────────────────────────────────────────────────────────────

def run_cron_safe() -> None:
    """
    Lock-safe cron entry point.
    Writes a lock file so parallel runs are skipped (not stacked).
    Safe to schedule every 15 min — exits immediately if another run is in progress.
    """
    import os, sys, tempfile, time

    lock_path = os.path.join(tempfile.gettempdir(), "erp_alerts.lock")

    # Check lock file — if younger than 10 min, skip
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < 600:  # 10 minute guard
            _log.info("[erp_alerts.cron] Previous run still active — skipping")
            return
        else:
            os.remove(lock_path)  # Stale lock — remove and proceed

    try:
        # Write lock
        with open(lock_path, "w") as lf:
            lf.write(str(os.getpid()))

        _log.info("[erp_alerts.cron] Starting alert detection")
        results = run_alert_detection()
        total   = sum(results.values())
        _log.info(f"[erp_alerts.cron] Done — {total} alerts flagged: {results}")

    except Exception as e:
        _log.error(f"[erp_alerts.cron] Failed: {e}")
    finally:
        # Always remove lock
        try:
            os.remove(lock_path)
        except Exception:
            pass


if __name__ == "__main__":
    import logging as _main_log
    _main_log.basicConfig(
        level=_main_log.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run_cron_safe()
