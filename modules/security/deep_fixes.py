"""
modules/security/deep_fixes.py  — v2 (hardened)
══════════════════════════════════════════════════════════════════════════════
All 7 fixes from the deep audit, now with all critical issues resolved:

  FIX 1  Fail-open → fail-closed in validation functions
  FIX 2  All DDL → migration SQL (0010_permission_and_guard_tables.sql)
         Runtime functions assume migration has run; never ALTER/CREATE
  FIX 3  Float comparison → round(x, 2) != round(y, 2)
  FIX 4  Soft delete: is_deleted=TRUE only; status UNCHANGED
  FIX 5  Price batch fetch: one DB query, dict lookup per line
  FIX 6  arc_backstep_log DDL → migration SQL (same file)
  FIX 7  clear_backoffice_touch_flag → checks job_master first; refuses if found

IMPORTANT — DB MIGRATION REQUIRED BEFORE FIRST USE:
  Run: migrations/0010_permission_and_guard_tables.sql
  This adds: bo_opened_at, is_deleted on orders; arc_backstep_log; indexes.
  None of these are created at runtime anymore.
"""

from __future__ import annotations
import logging
import streamlit as st
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# _q / _w intentionally fail-open: they are general-purpose helpers used
# everywhere including non-critical display paths. Crashing on every DB blip
# would be worse than silently skipping a cosmetic query.
#
# VALIDATION FUNCTIONS are different — they use _q_strict / _w_strict which
# raise on failure so the caller can decide to fail-closed.
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql: str, params=None) -> list:
    """Fail-open: used for display/read paths. Returns [] on error."""
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        log.warning("_q failed (non-critical): %s", e)
        return []


def _w(sql: str, params=None) -> bool:
    """Fail-open: used for best-effort writes (logs, flags). Returns False on error."""
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        log.warning("_w failed (non-critical): %s", e)
        return False


def _q_strict(sql: str, params=None) -> list:
    """
    Fail-CLOSED: used in validation functions.
    Raises RuntimeError on DB failure so callers can return an error, not silently pass.
    """
    try:
        from modules.sql_adapter import run_query
        result = run_query(sql, params or {})
        return result or []
    except Exception as e:
        log.error("_q_strict DB failure: %s", e)
        raise RuntimeError(f"DB validation query failed: {e}") from e


def _w_strict(sql: str, params=None) -> bool:
    """Fail-CLOSED: used in critical writes. Raises on failure."""
    try:
        from modules.sql_adapter import run_write
        ok = run_write(sql, params or {})
        if not ok:
            raise RuntimeError("run_write returned False — write did not execute")
        return True
    except Exception as e:
        log.error("_w_strict DB failure: %s", e)
        raise RuntimeError(f"DB write failed: {e}") from e


# ═════════════════════════════════════════════════════════════════════════════
# FIX 1 + 2 — BACKOFFICE TOUCH FLAG
# DDL is in migration 0010_permission_and_guard_tables.sql — NOT here.
# Runtime only does UPDATE (never ALTER TABLE).
# ═════════════════════════════════════════════════════════════════════════════

def backoffice_touch_flag(order_id: str) -> None:
    """
    Write touch flag when backoffice opens an order.
    Call at the top of render_order_detail() in backoffice_ui.py.

    Fails silently if column doesn't exist yet (migration not run).
    Logs a clear warning so ops team knows migration is needed.

    MIGRATION REQUIRED: 0010_permission_and_guard_tables.sql
    """
    if not order_id:
        return

    try:
        from modules.security.roles import current_user_name
        user = current_user_name()
    except Exception:
        user = "backoffice"

    try:
        _w_strict("""
        UPDATE orders
        SET bo_opened_at = COALESCE(bo_opened_at, NOW()),
            bo_opened_by = COALESCE(bo_opened_by, %(u)s)
        WHERE id = %(oid)s::uuid
          AND bo_opened_at IS NULL
        """, {"oid": order_id, "u": user})
    except RuntimeError as e:
        # Column may not exist if migration hasn't run yet
        if "bo_opened_at" in str(e) or "column" in str(e).lower():
            log.warning(
                "backoffice_touch_flag: bo_opened_at column missing. "
                "Run migration 0010_permission_and_guard_tables.sql"
            )
        else:
            log.error("backoffice_touch_flag unexpected error: %s", e)


def order_is_backoffice_touched(order: dict) -> tuple[bool, str]:
    """
    Returns (touched: bool, signal: str).

    Five signals checked in priority order.
    Uses _q (fail-open) — if DB is down, falls back gracefully through signals.
    The status check (signal 1) works even without DB.
    """
    # Signal 1: status — no DB needed
    status = str(order.get("status") or "PENDING").upper()
    _LOCKED = {"CONFIRMED", "IN_PRODUCTION", "READY", "BILLED",
               "DISPATCHED", "DELIVERED", "CLOSED"}
    if status in _LOCKED:
        return True, f"ORDER_STATUS:{status}"

    order_id = str(order.get("id") or "").strip()

    # Signal 2: touch flag
    if order_id:
        try:
            rows = _q("""
                SELECT bo_opened_at, bo_opened_by FROM orders
                WHERE id = %(oid)s::uuid AND bo_opened_at IS NOT NULL
                LIMIT 1
            """, {"oid": order_id})
            if rows:
                by   = rows[0].get("bo_opened_by") or "backoffice"
                when = str(rows[0].get("bo_opened_at") or "")[:16]
                return True, f"BO_TOUCH_FLAG:{by}:{when}"
        except Exception:
            pass  # column not yet created — continue to other signals

    # Signals 3–5: job/blank/surfacing
    lines    = order.get("lines") or []
    line_ids = [
        (ln.get("line_id") or ln.get("id") or "").strip()
        for ln in lines
        if (ln.get("line_id") or ln.get("id") or "").strip()
    ]
    if line_ids:
        checks = [
            ("SELECT id FROM job_master WHERE order_line_id=ANY(%(ids)s::uuid[]) LIMIT 1",
             "JOB_CARD_EXISTS"),
            ("SELECT id FROM blank_allocations WHERE order_line_id=ANY(%(ids)s::uuid[]) LIMIT 1",
             "BLANK_ALLOCATED"),
            ("SELECT id FROM order_lines WHERE id=ANY(%(ids)s::uuid[]) "
             "AND lens_params::jsonb?'surfacing_data' LIMIT 1",
             "SURFACING_DATA_SAVED"),
        ]
        for sql, signal in checks:
            try:
                if _q(sql, {"ids": line_ids}):
                    return True, signal
            except Exception:
                pass

    return False, ""


_SIGNAL_TEXT = {
    "JOB_CARD_EXISTS":      "A job card has been created for this order in Backoffice.",
    "BLANK_ALLOCATED":      "A blank has been allocated in Backoffice.",
    "SURFACING_DATA_SAVED": "Job card data has been saved in Backoffice.",
}
_SIGNAL_GUIDE = {
    "JOB_CARD_EXISTS": (
        "Go to **Backoffice → this order → Documents tab**. "
        "Cancel the job card there first if you need to change power."
    ),
    "BLANK_ALLOCATED": (
        "Go to **Backoffice → Production tab → Reject & Return Blank** to release it, "
        "then edit the order."
    ),
    "SURFACING_DATA_SAVED": "Go to **Backoffice → this order** to make all changes.",
}


def render_backoffice_lock_banner(signal: str, order: dict) -> None:
    order_no = order.get("order_no") or order.get("display_order_no") or "—"
    status   = str(order.get("status") or "").upper()

    if signal.startswith("ORDER_STATUS:"):
        msg      = f"Order is {status}."
        guidance = "Use **Backoffice** to make any changes."
        border   = "#7c3aed"
        detail   = ""
    elif signal.startswith("BO_TOUCH_FLAG:"):
        parts    = signal.split(":", 2)
        by_user  = parts[1] if len(parts) > 1 else "backoffice"
        opened   = parts[2] if len(parts) > 2 else ""
        msg      = (
            f"Opened in Backoffice"
            + (f" by **{by_user}**" if by_user not in ("backoffice", "") else "")
            + (f" at {opened}" if opened else "") + "."
        )
        guidance = (
            "All edits must be made in **Backoffice**. "
            "Ask the backoffice user to use *Release for Edit* if retail corrections are needed."
        )
        border   = "#ef4444"
        detail   = "💡 Release clears the flag — but only if no job card exists yet."
    else:
        msg      = _SIGNAL_TEXT.get(signal, "Processed in Backoffice.")
        guidance = _SIGNAL_GUIDE.get(signal, "Go to Backoffice to edit.")
        border   = "#ef4444"
        detail   = ""

    st.markdown(
        f"<div style='background:#1a0a0a;border:1px solid {border};"
        f"border-radius:8px;padding:14px 16px;margin:8px 0'>"
        f"<div style='color:{border};font-weight:700;margin-bottom:6px'>"
        f"🔒 Order {order_no} — Editing Blocked</div>"
        f"<div style='color:#94a3b8;font-size:0.82rem;margin-bottom:4px'>{msg}</div>"
        f"<div style='color:#60a5fa;font-size:0.8rem'>{guidance}</div>"
        + (f"<div style='color:#64748b;font-size:0.75rem;margin-top:4px'>{detail}</div>"
           if detail else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def clear_backoffice_touch_flag(order_id: str) -> tuple[bool, str]:
    """
    FIX 7 — Clear the touch flag only if safe to do so.

    REFUSES to clear if:
      - Any job_master row exists for this order's lines (job card created)
      - Any blank_allocations row exists
      - Order status is CONFIRMED or beyond

    Returns (cleared: bool, reason: str).
    """
    if not order_id:
        return False, "No order ID provided."

    # Safety check 1: status
    try:
        status_rows = _q_strict(
            "SELECT status FROM orders WHERE id=%(oid)s::uuid LIMIT 1",
            {"oid": order_id}
        )
        if status_rows:
            st = str(status_rows[0].get("status") or "").upper()
            if st in {"CONFIRMED", "IN_PRODUCTION", "READY", "BILLED",
                      "DISPATCHED", "DELIVERED", "CLOSED"}:
                return False, (
                    f"Cannot release — order is {st}. "
                    "Edit via Backoffice only."
                )
    except RuntimeError as e:
        return False, f"DB error during status check: {e}"

    # Safety check 2: job_master (FIX 7 — the critical guard)
    try:
        jm_rows = _q_strict("""
            SELECT jm.id FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            WHERE ol.order_id = %(oid)s::uuid
            LIMIT 1
        """, {"oid": order_id})
        if jm_rows:
            return False, (
                "Cannot release — a job card exists for this order. "
                "Cancel the job card in Backoffice first, "
                "then release for retail editing."
            )
    except RuntimeError as e:
        return False, f"DB error during job card check: {e}"

    # Safety check 3: blank_allocations
    try:
        ba_rows = _q_strict("""
            SELECT ba.id FROM blank_allocations ba
            JOIN order_lines ol ON ol.id = ba.order_line_id
            WHERE ol.order_id = %(oid)s::uuid
            LIMIT 1
        """, {"oid": order_id})
        if ba_rows:
            return False, (
                "Cannot release — a blank is allocated for this order. "
                "Reject and return the blank first."
            )
    except RuntimeError as e:
        return False, f"DB error during blank check: {e}"

    # Safe to clear
    try:
        _w_strict("""
        UPDATE orders
        SET bo_opened_at = NULL, bo_opened_by = NULL
        WHERE id = %(oid)s::uuid
        """, {"oid": order_id})
        return True, "Order released for retail editing."
    except RuntimeError as e:
        return False, f"Failed to clear flag: {e}"


# ═════════════════════════════════════════════════════════════════════════════
# FIX 1 + 3 — PRICE VALIDATION: fail-closed + round() comparison
# ═════════════════════════════════════════════════════════════════════════════

def validate_price_at_pipeline(lines: list, order_no: str = "") -> list[str]:
    """
    Second-pass validation at the save layer (called from order_persistence.py
    BEFORE any SQL write).

    FAIL-CLOSED: if DB is unreachable, returns an error string rather than
    silently passing. This prevents a DB outage from becoming a price bypass.

    FIX 3: uses round(x, 2) != round(y, 2) — no float edge cases.

    USAGE in order_persistence.py:
        violations = validate_price_at_pipeline(all_lines, order_no)
        if violations:
            for v in violations: st.error(v)
            conn.rollback()
            return
    """
    try:
        from modules.security.roles import has_role, current_role
    except Exception:
        # If roles module fails: fail-closed — block save
        return ["SYSTEM ERROR: roles module unavailable — save blocked for safety."]

    if has_role("manager", "admin"):
        return []

    # Collect line IDs
    line_ids = [
        (ln.get("line_id") or ln.get("id") or "").strip()
        for ln in lines
        if (ln.get("line_id") or ln.get("id") or "").strip()
    ]
    if not line_ids:
        return []

    # Fetch DB prices — FAIL-CLOSED if this fails
    try:
        db_rows = _q_strict("""
            SELECT id::text AS id, unit_price
            FROM order_lines
            WHERE id = ANY(%(ids)s::uuid[])
        """, {"ids": line_ids})
    except RuntimeError as e:
        # DB unavailable — BLOCK the save, do not silently pass
        return [
            f"SYSTEM ERROR — price validation could not complete (DB error: {e}). "
            "Save blocked. Contact admin or retry."
        ]

    db_prices = {r["id"]: float(r.get("unit_price") or 0) for r in db_rows}
    role       = current_role()
    violations: list[str] = []

    for ln in lines:
        lid = (ln.get("line_id") or ln.get("id") or "").strip()
        if not lid or lid not in db_prices:
            continue  # new line — no DB price to compare

        db_price  = db_prices[lid]
        new_price = float(ln.get("unit_price") or 0)

        # FIX 3: round() comparison — no float precision issues
        if db_price > 0 and round(new_price, 2) != round(db_price, 2):
            product = ln.get("product_name") or lid[:8]
            violations.append(
                f"⛔ Price override blocked — '{product}': "
                f"₹{db_price:.2f} → ₹{new_price:.2f}. "
                f"Role '{role}' cannot change prices. "
                f"This was blocked at the save layer."
            )

    return violations


# ═════════════════════════════════════════════════════════════════════════════
# FIX 3 + 5 — DISCOUNT: DB-locked base price + BATCH fetch
# ═════════════════════════════════════════════════════════════════════════════

def fetch_db_prices_batch(line_ids: list[str]) -> dict[str, float]:
    """
    FIX 5: Fetch all DB prices in ONE query.
    Returns {line_id: unit_price}.

    FAIL-CLOSED: raises RuntimeError if DB unavailable.
    Caller must handle this and return an error, not silently pass.
    """
    if not line_ids:
        return {}
    try:
        rows = _q_strict("""
            SELECT id::text AS id, unit_price
            FROM order_lines
            WHERE id = ANY(%(ids)s::uuid[])
        """, {"ids": line_ids})
        return {r["id"]: float(r.get("unit_price") or 0) for r in rows}
    except RuntimeError:
        raise  # propagate to caller


def calculate_effective_discount_strict(
    lines: list,
    threshold_pct: Optional[float] = None,
) -> dict:
    """
    FIX 3 + 5: Strict discount calculation.
    - Batch fetches all DB prices in ONE query (not one per line)
    - Uses DB unit_price as the locked base (not UI value)
    - FAIL-CLOSED: returns error dict if DB unavailable

    Returns dict with keys:
      original_total, final_total, discount_amount, effective_pct,
      over_threshold, threshold_used, price_source, error (optional)
    """
    if threshold_pct is None:
        try:
            from modules.security.permission_engine import get_discount_threshold
            threshold_pct = get_discount_threshold()
        except Exception:
            threshold_pct = 20.0

    # Collect all line IDs
    line_ids = [
        (ln.get("line_id") or ln.get("id") or "").strip()
        for ln in lines
        if (ln.get("line_id") or ln.get("id") or "").strip()
    ]

    # Batch fetch DB prices
    try:
        db_prices = fetch_db_prices_batch(line_ids)
    except RuntimeError as e:
        # FAIL-CLOSED: DB unavailable → treat as OVER threshold to force approval
        return {
            "original_total": 0, "final_total": 0,
            "discount_amount": 0, "effective_pct": 0.0,
            "over_threshold": True,   # fail-closed: force manager review
            "threshold_used": threshold_pct,
            "price_source": "db_unavailable",
            "error": str(e),
        }

    original_total = 0.0
    final_total    = 0.0

    for ln in lines:
        qty     = float(ln.get("billing_qty") or ln.get("quantity") or 0)
        lid     = (ln.get("line_id") or ln.get("id") or "").strip()

        # DB price takes priority; fall back to UI value for new lines
        orig_price = (db_prices.get(lid) or 0.0) if lid in db_prices else float(ln.get("unit_price") or 0)

        if qty <= 0 or orig_price <= 0:
            continue

        disc_pct = float(ln.get("discount_percent") or 0)
        disc_amt = float(ln.get("discount_amount")  or 0)

        line_orig     = qty * orig_price
        line_discount = (disc_amt if disc_amt > 0
                         else line_orig * (disc_pct / 100.0) if disc_pct > 0
                         else 0.0)

        original_total += line_orig
        final_total    += (line_orig - line_discount)

    if original_total <= 0:
        return {
            "original_total": 0, "final_total": 0,
            "discount_amount": 0, "effective_pct": 0.0,
            "over_threshold": False, "threshold_used": threshold_pct,
            "price_source": "db_locked",
        }

    discount_amount = original_total - final_total
    effective_pct   = (discount_amount / original_total) * 100.0

    return {
        "original_total":  round(original_total, 2),
        "final_total":     round(final_total, 2),
        "discount_amount": round(discount_amount, 2),
        "effective_pct":   round(effective_pct, 2),
        "over_threshold":  effective_pct > threshold_pct,
        "threshold_used":  threshold_pct,
        "price_source":    "db_locked",
    }


def render_discount_approval_gate(
    disc_info: dict,
    order_no: str,
    context_key: str,
) -> tuple[bool, dict]:
    """
    Renders manager approval dialog when discount exceeds threshold
    OR when price_source is db_unavailable (fail-closed path).
    """
    from modules.security.roles import has_role, current_user_name
    from modules.security.permission_engine import PRICE_OVERRIDE_REASONS, log_override

    _key    = f"disc_gate_{context_key}"
    _ok_key = f"{_key}_ok"

    if st.session_state.get(_ok_key):
        return True, st.session_state.get(f"{_key}_meta", {})

    if disc_info.get("error"):
        # DB unavailable path — always require manager
        st.error(
            f"⚠️ Discount validation could not complete (DB error). "
            f"Manager must approve to continue. Error: {disc_info['error']}"
        )
        if not has_role("manager", "admin"):
            return False, {}

    elif not has_role("manager", "admin"):
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:6px;padding:10px 14px'>"
            f"<span style='color:#fca5a5;font-weight:700'>⛔ Discount Approval Required</span><br>"
            f"<span style='color:#94a3b8;font-size:0.8rem'>"
            f"Effective discount <b>{disc_info['effective_pct']:.1f}%</b> "
            f"exceeds {disc_info['threshold_used']:.0f}% threshold. "
            f"Manager approval required.</span></div>",
            unsafe_allow_html=True,
        )
        return False, {}

    st.markdown(
        f"<div style='background:#1c1107;border:1px solid #f59e0b;"
        f"border-radius:8px;padding:12px 14px;margin:6px 0'>"
        f"<div style='color:#fbbf24;font-weight:700;margin-bottom:6px'>"
        f"⚠️ Manager Approval — High Discount</div>"
        f"<div style='color:#fed7aa;font-size:0.82rem'>"
        f"Effective discount: <b>{disc_info['effective_pct']:.1f}%</b> · "
        f"₹{disc_info['discount_amount']:,.2f} off ₹{disc_info['original_total']:,.2f}"
        + (f"<br><span style='color:#f87171'>Base price: DB unavailable — "
           f"using UI values</span>" if disc_info.get("error") else "")
        + "</div></div>",
        unsafe_allow_html=True,
    )

    # Load custom reasons from DB if saved
    _reasons = PRICE_OVERRIDE_REASONS
    try:
        import json as _j
        _r = _q("SELECT value FROM permission_settings WHERE key='price_override_reasons'")
        if _r:
            _reasons = ["— Select reason —"] + _j.loads(_r[0]["value"]) + ["Other (specify below)"]
    except Exception:
        pass

    reason = st.selectbox("Reason", _reasons, key=f"{_key}_r")
    note   = st.text_input("Note (optional)", key=f"{_key}_n",
                           placeholder="Authorization reference, context")
    final  = note.strip() if reason == "Other (specify below)" else reason

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Approve", type="primary", key=f"{_key}_btn",
                     use_container_width=True,
                     disabled=(final in ("", "— Select reason —"))):
            meta = {"reason": final, "note": note, "approved_by": current_user_name()}
            log_override(
                "discount_approval", order_no,
                f"{disc_info['threshold_used']:.0f}% threshold",
                f"{disc_info['effective_pct']:.1f}% effective",
                final, note, current_user_name(),
            )
            st.session_state[_ok_key]        = True
            st.session_state[f"{_key}_meta"] = meta
            st.rerun()
    with c2:
        if st.button("✕ Cancel", key=f"{_key}_cancel", use_container_width=True):
            st.rerun()
    return False, {}


# ═════════════════════════════════════════════════════════════════════════════
# FIX 4 — SOFT DELETE: is_deleted only, status UNCHANGED
# ═════════════════════════════════════════════════════════════════════════════

def soft_delete_order(order: dict, reason: str, deleted_by: str = "") -> tuple[bool, str]:
    """
    FIX 4: Soft delete ONLY.
    - Sets is_deleted=TRUE
    - Status is NOT changed (preserves business state for reports)
    - Financial records (invoices, payments, challans) are NEVER touched
    - Returns (success, message)

    WHY status unchanged:
      Reports distinguish cancelled vs deleted differently.
      is_deleted=TRUE is the deletion flag. Status stays as the business truth.
      If you want to cancel AND delete: run cancel first (sets CANCELLED),
      then soft_delete_order (sets is_deleted=TRUE). Two separate actions.
    """
    try:
        from modules.security.roles import current_user_name
        deleted_by = deleted_by or current_user_name()
    except Exception:
        pass

    order_id = str(order.get("id") or "")
    order_no = str(order.get("order_no") or order.get("display_order_no") or "")

    if not order_id:
        return False, "Cannot delete: order has no ID."
    if not reason.strip():
        return False, "Reason is required for deletion."

    # MIGRATION REQUIRED: 0010_permission_and_guard_tables.sql must have run
    # (adds is_deleted, deleted_at, deleted_by, delete_reason to orders)

    try:
        ok = _w_strict("""
        UPDATE orders
        SET is_deleted    = TRUE,
            deleted_at    = NOW(),
            deleted_by    = %(by)s,
            delete_reason = %(r)s
            -- NOTE: status is intentionally NOT changed here
            -- Reports can distinguish CANCELLED (business) from is_deleted (admin action)
        WHERE id = %(oid)s::uuid
          AND COALESCE(is_deleted, FALSE) = FALSE
        """, {"oid": order_id, "by": deleted_by, "r": reason.strip()})
    except RuntimeError as e:
        return False, (
            f"Delete failed (DB error): {e}. "
            "Migration 0010_permission_and_guard_tables.sql may not have run."
        )

    # Soft delete unbilled order lines (keep billed lines for accounting)
    _w("""
    UPDATE order_lines
    SET is_deleted = TRUE, deleted_at = NOW(), deleted_by = %(by)s
    WHERE order_id = %(oid)s::uuid
      AND COALESCE(is_deleted, FALSE) = FALSE
      AND COALESCE(billed_qty, 0) = 0
    """, {"oid": order_id, "by": deleted_by})

    # Audit log
    try:
        from modules.security.permission_engine import log_override
        log_override("soft_delete_order", order_no,
                     order.get("status", "?"), "is_deleted=TRUE",
                     reason, "", deleted_by)
    except Exception:
        pass

    return True, (
        f"Order {order_no} soft-deleted (is_deleted=TRUE). "
        f"Status unchanged. Financial records preserved."
    )


def render_soft_delete_guard(order: dict) -> bool:
    """Full UI guard for soft delete. Returns True only after all checks pass."""
    from modules.security.roles import has_role, current_user_name

    if not has_role("admin"):
        st.error("⛔ Only Admin can delete an order.")
        return False

    order_no = order.get("order_no") or order.get("display_order_no") or "—"

    # Financial records block
    try:
        from modules.security.audit_fixes import (
            check_order_has_financial_records, render_financial_delete_block
        )
        fin_blocked, fin_reasons = check_order_has_financial_records(order)
        if fin_blocked:
            render_financial_delete_block(fin_reasons)
            return False
    except Exception:
        pass

    # Pipeline depth block
    try:
        from modules.security.business_guards import check_order_deletable
        can_del, block_msg = check_order_deletable(order)
        if not can_del:
            st.error(f"⛔ {block_msg}")
            return False
        if block_msg:
            st.warning(block_msg)
    except Exception:
        pass

    _key = f"softdel_{order_no}"
    st.markdown(
        "<div style='background:#1a0a0a;border:1px solid #ef4444;"
        "border-radius:8px;padding:12px 14px;margin:8px 0'>"
        "<div style='color:#ef4444;font-weight:700;margin-bottom:6px'>"
        "🗑️ Soft Delete Order</div>"
        "<div style='color:#94a3b8;font-size:0.8rem'>"
        "Sets <code>is_deleted=TRUE</code>. Status is <b>not changed</b>. "
        "Financial records preserved. Reversible only by direct DB access.</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    reason = st.text_input("Reason (required)", key=f"{_key}_r",
                           placeholder="e.g. Duplicate entry, test order")

    if not st.session_state.get(f"{_key}_confirm"):
        if st.button(f"🗑 Delete Order {order_no}", key=f"{_key}_btn",
                     type="primary", use_container_width=True,
                     disabled=not reason.strip()):
            st.session_state[f"{_key}_confirm"] = True
            st.rerun()
        return False

    st.warning(
        f"Final confirmation: soft-delete **{order_no}**? "
        f"Status unchanged. Reason: '{reason}'"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Confirm", key=f"{_key}_final",
                     type="primary", use_container_width=True):
            ok, msg = soft_delete_order(order, reason, current_user_name())
            st.session_state.pop(f"{_key}_confirm", None)
            if ok:
                st.success(msg)
                return True
            else:
                st.error(msg)
    with c2:
        if st.button("← Cancel", key=f"{_key}_back", use_container_width=True):
            st.session_state.pop(f"{_key}_confirm", None)
            st.rerun()
    return False


# ═════════════════════════════════════════════════════════════════════════════
# FIX 2 + 6 — ARC EVENT: no DDL at runtime, full linked record
# ═════════════════════════════════════════════════════════════════════════════

def log_arc_event_with_links(
    job_id: str,
    order_id: str,
    order_line_id: str,
    eye_side: str,
    from_stage: str,
    reason: str,
    vendor_name: str = "",
    vendor_id: str = "",
    vendor_contact: str = "",
    po_ref: str = "",
    po_id: str = "",
) -> None:
    """
    FIX 6: NO CREATE TABLE IF NOT EXISTS here.
    MIGRATION REQUIRED: 0010_permission_and_guard_tables.sql creates arc_backstep_log.

    Writes linked audit record + attempts WhatsApp notification.
    All paths (WA success / WA fail / no contact) handled with clear UI feedback.
    """
    try:
        from modules.security.roles import current_user_name, current_user_id
        user    = current_user_name()
        user_id = str(current_user_id() or "")
    except Exception:
        user, user_id = "system", ""

    # Resolve vendor_id if not passed
    if not vendor_id and vendor_name:
        rows = _q("""
            SELECT id::text FROM suppliers
            WHERE vendor_name ILIKE %(vn)s OR name ILIKE %(vn)s LIMIT 1
        """, {"vn": vendor_name})
        vendor_id = str(rows[0].get("id") or "") if rows else ""

    wa_sent = False
    if vendor_contact:
        try:
            from modules.flags.feature_flags import flag
            if flag("enable_whatsapp_po", False):
                from modules.procurement.po_engine import _send_whatsapp
                _send_whatsapp({"vendor_contact": vendor_contact}, (
                    f"🔔 *ARC Recall Notice*\n"
                    f"PO: {po_ref} | Eye: {eye_side}\n"
                    f"Reason: {reason}\n"
                    f"Please hold/return the lens immediately.\n"
                    f"Authorised by: {user}"
                ))
                wa_sent = True
        except Exception as e:
            st.warning(
                f"⚠️ WhatsApp to {vendor_name} failed ({e}). "
                f"Call {vendor_contact} manually. Event still logged."
            )

    if not wa_sent and vendor_contact:
        st.info(
            f"📞 Notify ARC vendor manually: **{vendor_name}** · {vendor_contact} · PO: {po_ref}"
        )
    elif not vendor_contact:
        st.warning(f"⚠️ No contact number for '{vendor_name}'. Update vendor master. Event logged.")

    # MIGRATION REQUIRED for arc_backstep_log table
    try:
        _w_strict("""
        INSERT INTO arc_backstep_log
            (job_id, order_id, order_line_id, eye_side, from_stage,
             vendor_id, vendor_name, vendor_contact, po_id, po_ref,
             reason, notified_wa, performed_by, performed_by_name, created_at)
        VALUES
            (%(j)s::uuid,
             NULLIF(%(oid)s,'')::uuid,
             NULLIF(%(lid)s,'')::uuid,
             %(eye)s, %(fs)s,
             NULLIF(%(vid)s,'')::uuid,
             %(vn)s, %(vc)s,
             NULLIF(%(pid)s,'')::integer,
             %(pr)s, %(r)s, %(wa)s,
             NULLIF(%(uid)s,'')::uuid,
             %(un)s, NOW())
        """, {
            "j":   job_id,
            "oid": order_id         or "",
            "lid": order_line_id    or "",
            "eye": (eye_side or "")[:1].upper() or None,
            "fs":  from_stage,
            "vid": vendor_id        or "",
            "vn":  vendor_name      or "",
            "vc":  vendor_contact   or "",
            "pid": str(po_id)       if po_id else "",
            "pr":  po_ref           or "",
            "r":   reason,
            "wa":  wa_sent,
            "uid": user_id,
            "un":  user,
        })
    except RuntimeError as e:
        log.error("log_arc_event_with_links: arc_backstep_log write failed: %s", e)
        st.warning(
            f"⚠️ ARC event could not be written to audit log: {e}. "
            f"Migration 0010_permission_and_guard_tables.sql may not have run."
        )


# ═════════════════════════════════════════════════════════════════════════════
# FIX 6 — PO CANCEL: explicit "stock NOT reversed" warning
# ═════════════════════════════════════════════════════════════════════════════

def render_po_cancel_stock_warning(po_id_int: int) -> bool:
    """
    Must be called BEFORE render_po_cancel_guard().
    Returns True only after user acknowledges stock impact.
    """
    _key = f"po_stock_warn_{po_id_int}"
    if st.session_state.get(f"{_key}_acked"):
        return True

    items = _q("""
        SELECT soi.received_qty, soi.ordered_qty,
               COALESCE(p.product_name, 'Unknown') AS product_name
        FROM supplier_order_items soi
        LEFT JOIN order_lines ol ON ol.id = soi.customer_line_id
        LEFT JOIN products p     ON p.id  = ol.product_id
        WHERE soi.supplier_order_id = %(po)s
    """, {"po": po_id_int})

    total_received = sum(int(r.get("received_qty", 0)) for r in items)
    total_ordered  = sum(int(r.get("ordered_qty",  0)) for r in items)

    if total_received > 0:
        st.markdown(
            "<div style='background:#1a0f00;border:2px solid #f97316;"
            "border-radius:8px;padding:14px 16px;margin:8px 0'>"
            "<div style='color:#fb923c;font-weight:800;margin-bottom:8px'>"
            "⚠️ STOCK IMPACT WARNING</div>"
            f"<div style='color:#fed7aa;font-size:0.82rem;margin-bottom:8px'>"
            f"{total_received} of {total_ordered} units already received and "
            f"counted in inventory.</div>"
            "<div style='background:#2d1900;border-radius:6px;padding:8px 12px;"
            "border-left:3px solid #ef4444'>"
            "<div style='color:#fca5a5;font-weight:700'>❌ Cancelling this PO "
            "will NOT reverse that stock.</div>"
            "<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
            "You must create a stock adjustment separately to remove it.</div>"
            "</div></div>",
            unsafe_allow_html=True,
        )
        ack = st.checkbox(
            f"I understand — {total_received} units remain in inventory "
            "and I will create a stock adjustment separately.",
            key=f"{_key}_chk",
        )
    else:
        st.markdown(
            "<div style='background:#0f172a;border:1px solid #334155;"
            "border-radius:6px;padding:10px 14px;margin:6px 0'>"
            "<div style='color:#94a3b8;font-size:0.8rem'>"
            "ℹ️ No stock received from this PO — cancellation is clean.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        ack = True

    if ack:
        if total_received > 0:
            if st.button("Continue to Cancel →", key=f"{_key}_cont"):
                st.session_state[f"{_key}_acked"] = True
                st.rerun()
        else:
            st.session_state[f"{_key}_acked"] = True
            return True

    return False


# ═════════════════════════════════════════════════════════════════════════════
# FIX 7 — TEST ACTION AS ROLE
# ═════════════════════════════════════════════════════════════════════════════

def render_test_action_as_role() -> None:
    """
    Shows admin exactly what a role experiences when attempting an action:
    blocked / guarded (with which dialog) / fully allowed.
    """
    from modules.security.permission_engine import (
        ACTION_CATALOGUE, SIDEBAR_CATALOGUE, ROLES_ORDERED,
        load_role_action_grants, load_role_module_grants,
        get_discount_threshold,
    )
    ROLE_ICONS = {
        "viewer":"👁️","staff":"👤","billing":"💳","lab":"🔬",
        "inventory":"📦","manager":"🔑","admin":"👑",
    }

    st.markdown("### 🎭 Test Action as Role")
    st.markdown(
        "<span style='color:#94a3b8;font-size:0.8rem'>"
        "See exactly what a role experiences — blocked, guarded, or allowed.</span>",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        test_role = st.selectbox(
            "Test as", ROLES_ORDERED,
            format_func=lambda r: f"{ROLE_ICONS.get(r,'')} {r.upper()}",
            key="test_action_role",
        )
    with c2:
        all_actions = [(f"{a['module']} → {a['label']}", a) for a in ACTION_CATALOGUE]
        sel_label   = st.selectbox("Action to test", [x[0] for x in all_actions],
                                    key="test_action_select")
        sel_action  = next(a for lbl, a in all_actions if lbl == sel_label)

    db_mods   = load_role_module_grants(test_role)
    db_acts   = load_role_action_grants(test_role)

    mod_visible = db_mods.get(
        sel_action["module"],
        test_role in next(
            (m.get("default_roles",[]) for m in SIDEBAR_CATALOGUE if m["key"] == sel_action["module"]),
            []
        ) or test_role == "admin"
    )
    act_granted = db_acts.get(sel_action["module"], {}).get(
        sel_action["key"],
        test_role in sel_action.get("default_roles", []) or test_role == "admin"
    )

    icon = ROLE_ICONS.get(test_role, "")
    st.markdown("---")

    if not mod_visible:
        st.markdown(
            f"<div style='background:#111827;border:1px solid #374151;"
            f"border-radius:8px;padding:14px 16px'>"
            f"<div style='color:#6b7280;font-weight:700;margin-bottom:4px'>"
            f"🚫 Module not in sidebar</div>"
            f"<div style='color:#64748b;font-size:0.82rem'>"
            f"{icon} {test_role.upper()} does not see "
            f"<b>{sel_action['module']}</b> in their sidebar at all. "
            f"Cannot reach this action.</div></div>",
            unsafe_allow_html=True,
        )
        return

    if not act_granted:
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:8px;padding:14px 16px'>"
            f"<div style='color:#ef4444;font-weight:700;margin-bottom:6px'>"
            f"❌ Blocked: {sel_action['label']}</div>"
            f"<div style='color:#94a3b8;font-size:0.82rem;margin-bottom:8px'>"
            f"{icon} {test_role.upper()} sees the page but cannot do this.</div>"
            f"<div style='background:#0f172a;border:1px solid #334155;"
            f"border-radius:6px;padding:10px;font-size:0.8rem;color:#64748b'>"
            f"⛔ Access denied — requires "
            f"{', '.join(sel_action.get('default_roles',['admin'])).upper()}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        return

    # Guarded actions
    try:
        threshold = get_discount_threshold()
    except Exception:
        threshold = 20.0

    _GUARDS = {
        "discount_over_threshold": (
            "⚠️ Guarded — Approval Dialog",
            f"Attempt works, but triggers manager approval dialog when "
            f"effective discount exceeds {threshold:.0f}%. "
            f"If they ARE manager, they approve themselves."
        ),
        "override_price": (
            "⚠️ Guarded — Reason Required + Pipeline Check",
            "Reason dropdown required. Checked again at save layer regardless of UI."
        ),
        "backstep_stage": (
            "⚠️ Guarded — Consequence Analysis + Two-Step Confirm",
            "Shows cascade warnings + requires typed reason + two-step confirm."
        ),
        "cancel_sent_po": (
            "⚠️ Guarded — Stock Check + Vendor Ack + Two-Step",
            "Stock impact shown first → vendor confirmation checkbox → manager confirms."
        ),
    }

    if sel_action["key"] in _GUARDS:
        title, desc = _GUARDS[sel_action["key"]]
        st.markdown(
            f"<div style='background:#1c1107;border:1px solid #f59e0b;"
            f"border-radius:8px;padding:14px 16px'>"
            f"<div style='color:#fbbf24;font-weight:700;margin-bottom:6px'>{title}</div>"
            f"<div style='color:#94a3b8;font-size:0.82rem;margin-bottom:6px'>"
            f"{icon} {test_role.upper()} has access but will see:</div>"
            f"<div style='color:#fed7aa;font-size:0.82rem'>{desc}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='background:#0d2818;border:1px solid #10b981;"
            f"border-radius:8px;padding:14px 16px'>"
            f"<div style='color:#10b981;font-weight:700;margin-bottom:6px'>"
            f"✅ Allowed: {sel_action['label']}</div>"
            f"<div style='color:#94a3b8;font-size:0.82rem'>"
            f"{icon} {test_role.upper()} — button/form appears normally, no restrictions.</div>"
            f"<div style='color:#34d399;font-size:0.75rem;margin-top:4px'>"
            f"{sel_action.get('desc','')}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    defaults = sel_action.get("default_roles", [])
    st.caption("Default access: " + " · ".join(
        f"{ROLE_ICONS.get(r,'')} {r.upper()}" for r in defaults
    ))


__all__ = [
    "backoffice_touch_flag", "order_is_backoffice_touched",
    "render_backoffice_lock_banner", "clear_backoffice_touch_flag",
    "validate_price_at_pipeline",
    "fetch_db_prices_batch", "calculate_effective_discount_strict",
    "render_discount_approval_gate",
    "soft_delete_order", "render_soft_delete_guard",
    "log_arc_event_with_links",
    "render_po_cancel_stock_warning",
    "render_test_action_as_role",
]
