"""
modules/core/erp_stability.py
===============================
ERP Stability Layer.

Provides:
  - Soft delete          — orders never physically removed
  - Order locking        — immutable after billing complete
  - Price override guard — manager required for large discounts
  - Stock adjustments    — every manual stock change logged
  - Danger confirm       — double-confirmation for destructive actions
  - Admin panels         — recovery UI, audit viewer

USAGE:
    from modules.core.erp_stability import (
        soft_delete_order, restore_order,
        lock_order, assert_order_unlocked,
        check_discount, record_price_override,
        record_stock_adjustment,
        danger_confirm,
        render_deleted_orders_panel,
        render_audit_trail_panel,
    )
"""

import uuid
import streamlit as st
from typing import Optional

DISCOUNT_WARN_PCT  = 20
DISCOUNT_BLOCK_PCT = 50


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _write(sql, params):
    for fn in ("run_write", "execute_query"):
        try:
            import modules.sql_adapter as _m
            getattr(_m, fn)(sql, params)
            return True
        except AttributeError:
            continue
        except Exception:
            return False
    return False


def _query(sql, params=()):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _operator() -> str:
    try:
        from modules.security.roles import current_user_name
        return current_user_name()
    except Exception:
        u = st.session_state.get("user", "system")
        return u if isinstance(u, str) else u.get("name", "system")


def _audit(action, entity, entity_id, payload=None):
    try:
        from modules.backoffice.audit_logger import audit, AuditAction
        audit(AuditAction(action), entity=entity,
              entity_id=str(entity_id) if entity_id else None,
              payload=payload or {})
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# SOFT DELETE
# ═══════════════════════════════════════════════════════════════════════════════

def soft_delete_order(order_id: str, reason: str = "") -> bool:
    """Soft-delete an order. Recoverable. Audited."""
    op = _operator()
    ok = _write("""
        UPDATE orders
        SET deleted_at = NOW(), deleted_by = %s, delete_reason = %s,
            updated_at = NOW(), updated_by = %s
        WHERE id = %s AND deleted_at IS NULL
    """, (op, reason or "deleted by operator", op, str(order_id)))
    if ok:
        _audit("ORDER_DELETED", "orders", order_id,
               {"reason": reason, "deleted_by": op})
    return ok


def restore_order(order_id: str) -> bool:
    """Restore a soft-deleted order. Audited."""
    op = _operator()
    ok = _write("""
        UPDATE orders
        SET deleted_at = NULL, deleted_by = NULL, delete_reason = NULL,
            updated_at = NOW(), updated_by = %s
        WHERE id = %s AND deleted_at IS NOT NULL
    """, (op, str(order_id)))
    if ok:
        _audit("ORDER_RESTORED", "orders", order_id, {"restored_by": op})
    return ok


def get_deleted_orders(limit: int = 50):
    return _query("""
        SELECT id, order_no, order_type, deleted_at, deleted_by,
               delete_reason, total_value, patient_name
        FROM orders WHERE deleted_at IS NOT NULL
        ORDER BY deleted_at DESC LIMIT %s
    """, (limit,))


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER LOCKING
# ═══════════════════════════════════════════════════════════════════════════════

def lock_order(order_id: str) -> bool:
    """Lock an order after billing — prevents all edits."""
    op = _operator()
    ok = _write("""
        UPDATE orders SET is_locked = TRUE, locked_at = NOW(),
               locked_by = %s, updated_at = NOW()
        WHERE id = %s
    """, (op, str(order_id)))
    if ok:
        _audit("ORDER_LOCKED", "orders", order_id, {"locked_by": op})
    return ok


def assert_order_unlocked(order: dict) -> bool:
    """
    If order is locked, show error and return False.
    Use before any edit:
        if not assert_order_unlocked(order): return
    """
    if order.get("is_locked"):
        by = order.get("locked_by", "system")
        at = str(order.get("locked_at", ""))[:19]
        st.error(f"🔐 Order locked after billing by **{by}** at {at}. Contact admin to unlock.")
        return False
    return True


def is_order_locked(order_id: str) -> bool:
    rows = _query("SELECT is_locked FROM orders WHERE id = %s", (str(order_id),))
    return bool(rows and rows[0].get("is_locked"))


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE OVERRIDE GUARD
# ═══════════════════════════════════════════════════════════════════════════════

def check_discount(original: float, override: float) -> dict:
    """
    Returns {"pct": float, "level": "ok"|"warn"|"block", "message": str}
    Call when operator enters a manual price.
    """
    if not original or original <= 0:
        return {"pct": 0, "level": "ok", "message": ""}
    pct = ((original - override) / original) * 100
    if pct >= DISCOUNT_BLOCK_PCT:
        return {"pct": pct, "level": "block",
                "message": f"⛔ {pct:.0f}% discount requires manager approval"}
    if pct >= DISCOUNT_WARN_PCT:
        return {"pct": pct, "level": "warn",
                "message": f"⚠️ {pct:.0f}% discount — verify with manager"}
    return {"pct": pct, "level": "ok", "message": ""}


def record_price_override(line_id: str, original: float,
                          override: float, reason: str):
    """Record a price override on an order line with full audit trail."""
    op = _operator()
    _write("""
        UPDATE order_lines
        SET price_overridden = TRUE, original_price = %s,
            override_reason = %s, override_by = %s, override_at = NOW()
        WHERE id = %s
    """, (original, reason, op, str(line_id)))
    _audit("PRICE_OVERRIDDEN", "order_lines", line_id,
           {"original": original, "override": override, "reason": reason, "by": op})


# ═══════════════════════════════════════════════════════════════════════════════
# STOCK ADJUSTMENT LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def record_stock_adjustment(product_id: str, stock_id: Optional[str],
                            delta: int, reason: str,
                            approved_by: Optional[str] = None) -> bool:
    """
    Log a manual stock adjustment. Always call when changing inventory manually.
    delta: +ve = added, -ve = removed
    """
    op = _operator()
    ok = _write("""
        INSERT INTO stock_adjustments
            (id, product_id, stock_id, delta, reason, adjusted_by, approved_by, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
    """, (str(uuid.uuid4()), str(product_id),
          str(stock_id) if stock_id else None,
          int(delta), reason, op, approved_by or op))
    if ok:
        _audit("STOCK_ADJUSTED", "inventory_stock", stock_id,
               {"product_id": str(product_id), "delta": delta,
                "reason": reason, "by": op})
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# OPERATOR ERROR GUARDS
# ═══════════════════════════════════════════════════════════════════════════════

def danger_confirm(action_label: str, confirm_key: str,
                   warning_text: str = "") -> bool:
    """
    Double-confirmation checkbox for destructive actions.
    Returns True only when operator checks the box.

        if danger_confirm("Delete Order", f"del_{order_id}",
                          "This hides the order from all reports."):
            soft_delete_order(order_id, reason="operator deleted")
    """
    if warning_text:
        st.warning(warning_text)
    return st.checkbox(f"✅ I confirm: {action_label}",
                       key=f"_danger_confirm_{confirm_key}")


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN UI PANELS
# ═══════════════════════════════════════════════════════════════════════════════

def render_deleted_orders_panel():
    """Admin panel — view and restore soft-deleted orders."""
    try:
        from modules.security.roles import require_role, ADMIN, MANAGER
        require_role(ADMIN, MANAGER)
    except ImportError:
        pass

    st.markdown("#### 🗑️ Deleted Orders (Recoverable)")
    rows = get_deleted_orders(50)
    if not rows:
        st.success("✅ No deleted orders")
        return

    for r in rows:
        val = float(r.get("total_value") or 0)
        with st.expander(
            f"❌ {r.get('order_no','?')} — {r.get('patient_name','?')} "
            f"| ₹{val:,.0f} | {str(r.get('deleted_at',''))[:19]}"
        ):
            st.write(f"**Deleted by:** {r.get('deleted_by','?')}")
            st.write(f"**Reason:** {r.get('delete_reason') or '—'}")
            if st.button("♻️ Restore Order", key=f"restore_{r['id']}"):
                if restore_order(str(r["id"])):
                    st.success("✅ Restored")
                    st.rerun()
                else:
                    st.error("❌ Restore failed")


def render_audit_trail_panel(entity_id: Optional[str] = None, limit: int = 100):
    """Admin panel — filterable audit log viewer."""
    st.markdown("#### 📋 Audit Trail")

    c1, c2, c3 = st.columns(3)
    with c1:
        f_entity = st.selectbox("Entity",
            ["All", "orders", "order_lines", "inventory_stock", "payments"],
            key="_audit_fe")
    with c2:
        f_action = st.text_input("Action contains", key="_audit_fa")
    with c3:
        f_user = st.text_input("User", key="_audit_fu")

    sql    = "SELECT * FROM audit_logs WHERE 1=1"
    params = []
    if entity_id:
        sql += " AND entity_id = %s"; params.append(str(entity_id))
    if f_entity != "All":
        sql += " AND entity = %s"; params.append(f_entity)
    if f_action:
        sql += " AND action ILIKE %s"; params.append(f"%{f_action}%")
    if f_user:
        sql += " AND user_name ILIKE %s"; params.append(f"%{f_user}%")
    sql += f" ORDER BY created_at DESC LIMIT {limit}"

    rows = _query(sql, params)
    if not rows:
        st.info("No audit events found")
        return

    ICONS = {
        "ORDER_CREATED": "🟢", "ORDER_DELETED": "🔴", "ORDER_RESTORED": "🔵",
        "ORDER_LOCKED":  "🔒", "PRICE_OVERRIDDEN": "🟡", "STOCK_ADJUSTED": "🟠",
        "BILLING_SAVED": "🟢", "BILLING_LOCKED": "🔒", "PAYMENT_RECORDED": "🟢",
        "STAGE_ADVANCED": "⚙️", "LAB_ORDER_SENT": "🔬", "ALLOCATION_SAVED": "📦",
    }
    for r in rows:
        ts      = str(r.get("created_at", ""))[:19]
        action  = r.get("action", "")
        icon    = ICONS.get(action, "⚪")
        user    = r.get("user_name") or "system"
        entity  = r.get("entity", "")
        payload = r.get("payload") or {}
        st.markdown(f"{icon} `{ts}` **{action}** on `{entity}` — by **{user}**")
        if payload and payload != {}:
            with st.expander("Details", expanded=False):
                st.json(payload)
