"""
modules/security/business_guards.py
══════════════════════════════════════════════════════════════════════════════
6 enforcement rules — Streamlit UI helpers that wrap each guard.

Import from backoffice_ui, order_edit_view, supplier_panel, production_panel.

RULE SUMMARY:
  1. Backoffice CONFIRMED → retail edit blocked (go to backoffice)
  2. Price override       → manager only, reason required, logged
  3. Discount > threshold → manager only, reason required, logged
  4. Delete/cancel order  → pipeline depth check before allowing
  5. ARC reversal         → notify ARC vendor via WhatsApp/log
  6. Cancel SENT PO       → manager only, reason + confirm

DESIGN NOTE on rules 2 & 3 (combined workflow):
  Both are gated by the same reason dialog. If discount is over threshold
  AND price is overridden, one combined approval dialog appears — not two.
  This keeps the UX smooth and the audit log complete.
"""

from __future__ import annotations
import streamlit as st
from typing import Optional


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# RULE 1 — Backoffice CONFIRMED = no retail/orders edit
# ─────────────────────────────────────────────────────────────────────────────

def check_order_editable_from_retail(order: dict) -> tuple[bool, str]:
    """
    Returns (can_edit: bool, message: str).
    Call from order_edit_view BEFORE rendering any edit UI.

    If order is CONFIRMED or beyond: blocks edit and returns guidance.
    If billing user tries to edit: blocked with backoffice redirect message.
    """
    from modules.security.roles import has_role
    status = str(order.get("status") or "PENDING").upper()

    _LOCKED = {"CONFIRMED", "IN_PRODUCTION", "READY", "BILLED",
               "DISPATCHED", "DELIVERED", "CLOSED"}

    if status in _LOCKED:
        # Check if billing user (not lab/manager/admin)
        if not has_role("lab", "manager", "admin"):
            return False, (
                "🔒 This order has been saved in Backoffice (status: CONFIRMED). "
                "Billing users cannot edit confirmed orders. "
                "Go to **Backoffice → find this order** to request changes."
            )
        # Lab/manager: can edit in backoffice but not here
        return False, (
            f"🔒 Order is {status}. Edit via **Backoffice** only — "
            "open the order there and use the edit / release flow."
        )
    return True, ""


def render_retail_edit_block(order: dict) -> bool:
    """
    Renders the block banner if order is confirmed.
    Returns True if editing IS allowed, False if blocked.
    """
    can_edit, msg = check_order_editable_from_retail(order)
    if not can_edit:
        order_no = order.get("order_no") or order.get("display_order_no") or "—"
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:8px;padding:14px 16px;margin:8px 0'>"
            f"<div style='color:#ef4444;font-weight:700;font-size:0.95rem;margin-bottom:4px'>"
            f"🔒 Order {order_no} — Editing Blocked</div>"
            f"<div style='color:#94a3b8;font-size:0.82rem'>{msg}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    return can_edit


# ─────────────────────────────────────────────────────────────────────────────
# RULES 2 & 3 — Price override + High discount (combined approval dialog)
# ─────────────────────────────────────────────────────────────────────────────

def render_price_discount_guard(
    line: dict,
    new_price: float,
    new_discount_pct: float,
    order_no: str = "",
    context_key: str = "",
) -> tuple[bool, dict]:
    """
    Renders approval dialog if:
      - price changed from original (rule 2)
      - discount > threshold (rule 3)

    Returns (approved: bool, {reason, reason_note, approved_by}).
    If neither rule triggered, returns (True, {}) immediately — no dialog.

    USAGE in backoffice_ui (inside save flow):
        approved, meta = render_price_discount_guard(line, new_price, disc_pct, order_no, key)
        if not approved:
            st.stop()
        # proceed with save
        if meta:
            log_override("price_override", order_no, ...)
    """
    from modules.security.roles import has_role, current_user_name
    from modules.security.permission_engine import (
        get_discount_threshold, PRICE_OVERRIDE_REASONS, log_override
    )

    orig_price = float(line.get("unit_price") or 0)
    threshold  = get_discount_threshold()

    price_changed  = abs(new_price - orig_price) > 0.01 if orig_price > 0 else False
    disc_high      = new_discount_pct > threshold

    if not price_changed and not disc_high:
        return True, {}   # nothing to guard

    # Determine triggers
    triggers = []
    if price_changed:
        triggers.append(
            f"Price changed: ₹{orig_price:,.2f} → ₹{new_price:,.2f} "
            f"(Δ ₹{new_price - orig_price:+,.2f})"
        )
    if disc_high:
        triggers.append(
            f"Discount {new_discount_pct:.1f}% exceeds approval threshold ({threshold:.0f}%)"
        )

    # If billing user: block outright (manager must do this in backoffice)
    if not has_role("manager", "admin"):
        for t in triggers:
            st.markdown(
                f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
                f"border-radius:6px;padding:8px 14px;margin:4px 0;"
                f"color:#fca5a5;font-size:0.8rem'>⛔ {t} — Manager approval required. "
                f"Ask your manager to apply this change in Backoffice.</div>",
                unsafe_allow_html=True,
            )
        return False, {}

    # Manager+ — show approval dialog
    _guard_key = f"price_guard_{context_key or id(line)}"
    _approved_key = f"{_guard_key}_approved"

    if st.session_state.get(_approved_key):
        return True, st.session_state.get(f"{_guard_key}_meta", {})

    st.markdown(
        "<div style='background:#1c1107;border:1px solid #f59e0b;"
        "border-radius:8px;padding:12px 14px;margin:6px 0'>"
        "<div style='color:#fbbf24;font-weight:700;margin-bottom:6px'>"
        "⚠️ Approval Required</div>"
        + "".join(f"<div style='color:#fed7aa;font-size:0.8rem'>• {t}</div>" for t in triggers)
        + "</div>",
        unsafe_allow_html=True,
    )

    from modules.security.permission_engine import PRICE_OVERRIDE_REASONS as _REASONS
    # Load custom reasons from DB if saved
    try:
        import json as _j
        _r = _q("SELECT value FROM permission_settings WHERE key='price_override_reasons'")
        if _r:
            _REASONS = ["— Select reason —"] + _j.loads(_r[0]["value"]) + ["Other (specify below)"]
    except Exception:
        pass

    reason = st.selectbox(
        "Reason for override",
        _REASONS,
        key=f"{_guard_key}_reason",
        label_visibility="visible",
    )
    note = ""
    if reason == "Other (specify below)":
        note = st.text_input(
            "Specify reason", key=f"{_guard_key}_note",
            placeholder="Describe the override reason..."
        )
    final_reason = note.strip() if reason == "Other (specify below)" else reason

    c1, c2 = st.columns(2)
    with c1:
        if st.button(
            "✅ Approve & Continue",
            key=f"{_guard_key}_confirm",
            type="primary",
            use_container_width=True,
            disabled=(final_reason in ("", "— Select reason —")),
        ):
            meta = {
                "reason":      final_reason,
                "reason_note": note,
                "approved_by": current_user_name(),
            }
            # Log immediately
            log_override(
                action_type  = "price_override" if price_changed else "discount_approval",
                order_no     = order_no,
                original_val = f"price=₹{orig_price:.2f} disc={new_discount_pct-threshold:.1f}% over",
                new_val      = f"price=₹{new_price:.2f} disc={new_discount_pct:.1f}%",
                reason       = final_reason,
                reason_note  = note,
                approved_by  = current_user_name(),
            )
            st.session_state[_approved_key] = True
            st.session_state[f"{_guard_key}_meta"] = meta
            st.rerun()
    with c2:
        if st.button("✕ Cancel", key=f"{_guard_key}_cancel", use_container_width=True):
            st.rerun()

    return False, {}


# ─────────────────────────────────────────────────────────────────────────────
# RULE 4 — Delete / Cancel order: pipeline depth check
# ─────────────────────────────────────────────────────────────────────────────

def check_order_deletable(order: dict) -> tuple[bool, str]:
    """
    Returns (can_delete: bool, block_reason: str).
    Uses pipeline_guard depth to decide.
    Admin-only, and pipeline must allow it.
    """
    from modules.security.roles import has_role
    from modules.core.pipeline_guard import get_order_edit_permission

    if not has_role("admin"):
        return False, "Delete requires admin role."

    perm = get_order_edit_permission(order)
    depth = perm.max_depth

    if depth >= 7:
        return False, (
            "Cannot delete — goods have been received from supplier. "
            "Raise a supplier return instead."
        )
    if depth >= 6:
        return False, (
            "Cannot delete — supplier PO is in transit. "
            "Cancel the PO first via Supplier Panel, then delete."
        )
    if depth >= 4:
        return False, (
            "Cannot delete — job is in production/coating. "
            "Reject the blank and reset all jobs first."
        )
    if depth >= 3:
        return False, (
            "Cannot delete — a blank has been allotted. "
            "Cancel the job card (Reject & Return Blank) first."
        )
    if depth >= 1:
        return True, (
            "⚠️ A job card exists but no blank picked. "
            "Deleting will remove the job card record. Proceed with caution."
        )
    return True, ""


def render_delete_order_guard(order: dict) -> bool:
    """
    Renders the delete guard check.
    Returns True if deletion is allowed and user confirmed.
    Returns False if blocked or not yet confirmed.
    """
    from modules.security.roles import has_role, current_user_name
    from modules.security.permission_engine import log_override

    can_delete, msg = check_order_deletable(order)
    order_no = order.get("order_no") or order.get("display_order_no") or "—"

    if not can_delete:
        st.error(f"⛔ Cannot delete order {order_no}: {msg}")
        return False

    if msg:  # warning (depth 1)
        st.warning(msg)

    _gk = f"delete_guard_{order_no}"
    if not st.session_state.get(f"{_gk}_confirmed"):
        reason = st.text_input(
            "Reason for deletion (required)",
            key=f"{_gk}_reason",
            placeholder="Why is this order being deleted?",
        )
        if st.button("🗑 Confirm Delete Order", type="primary",
                     key=f"{_gk}_btn",
                     disabled=not reason.strip(),
                     use_container_width=True):
            log_override("delete_order", order_no, order.get("status",""), "DELETED",
                         reason, approved_by=current_user_name())
            st.session_state[f"{_gk}_confirmed"] = True
            return True
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# RULE 5 — ARC reversal → notify vendor
# ─────────────────────────────────────────────────────────────────────────────

def notify_arc_vendor_on_reversal(job_id: str, order_id: str,
                                   eye_side: str, reason: str) -> None:
    """
    Called after a confirmed ARC backstep.
    Looks up the ARC vendor for this order's PO and sends a WhatsApp/log notification.
    Non-blocking — failure is logged, never crashes the backstep.
    """
    try:
        # Find ARC vendor contact from supplier_orders linked to this job
        rows = _q("""
            SELECT so.vendor_name, so.vendor_contact, so.po_ref,
                   so.id AS po_id
            FROM supplier_orders so
            JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            JOIN job_master jm ON jm.order_line_id = soi.customer_line_id
            WHERE jm.id = %(j)s::uuid
              AND LOWER(COALESCE(so.po_type,'')) IN ('arc','external_lab','vendor')
            ORDER BY so.created_at DESC LIMIT 1
        """, {"j": job_id})

        vendor_name    = rows[0].get("vendor_name", "ARC Vendor") if rows else "ARC Vendor"
        vendor_contact = rows[0].get("vendor_contact", "") if rows else ""
        po_ref         = rows[0].get("po_ref", "") if rows else ""

        msg = (
            f"🔔 *ARC Recall Notice*\n"
            f"PO Ref: {po_ref}\n"
            f"Eye: {eye_side}\n"
            f"Reason: {reason}\n"
            f"Action required: Please hold / return this lens.\n"
            f"Contact us to confirm status."
        )

        # Attempt WhatsApp via feature flag
        try:
            from modules.flags.feature_flags import flag
            if flag("enable_whatsapp_po", False) and vendor_contact:
                from modules.procurement.po_engine import _send_whatsapp
                _send_whatsapp({"vendor_contact": vendor_contact}, msg)
        except Exception:
            pass  # WhatsApp optional

        # Always log regardless of WhatsApp
        _w("""
        INSERT INTO permission_override_log
            (action_type, order_no, original_val, new_val, reason, created_at)
        VALUES
            ('arc_vendor_notified', %(o)s, %(jid)s, %(ven)s, %(r)s, NOW())
        """, {"o": order_id, "jid": job_id, "ven": vendor_name, "r": reason})

        # Show in UI (non-blocking)
        if vendor_contact:
            st.info(
                f"📞 ARC vendor notified: **{vendor_name}** ({vendor_contact}) — "
                f"PO {po_ref}. Message: '{msg[:80]}...'"
            )
        else:
            st.warning(
                f"⚠️ ARC vendor **{vendor_name}** has no contact number saved. "
                f"Please notify them manually. PO Ref: {po_ref}"
            )

    except Exception as e:
        st.warning(f"⚠️ ARC vendor notification failed (non-critical): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# RULE 6 — Cancel SENT supplier PO: manager + reason + confirm
# ─────────────────────────────────────────────────────────────────────────────

def render_po_cancel_guard(po_id_int: int, po_ref: str, status: str,
                            vendor_name: str, order_id: str) -> bool:
    """
    Renders the guarded cancel-PO UI for SENT POs.
    Returns True only if manager confirmed and reason given.

    For DRAFT POs: allow immediate cancel (no guard needed — call directly).
    For SENT POs: this guard must be called.
    """
    from modules.security.roles import has_role, current_user_name
    from modules.security.permission_engine import log_override

    if status == "DRAFT":
        return True  # No guard for draft

    if not has_role("manager", "admin"):
        st.error(
            f"⛔ Cancelling a SENT PO requires Manager or Admin. "
            f"This PO ({po_ref}) has already been sent to the supplier."
        )
        return False

    _gk = f"po_cancel_guard_{po_id_int}"

    # Check if supplier may have dispatched
    _dispatched_risk = status in ("ACKNOWLEDGED", "PARTIAL")

    if _dispatched_risk:
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:6px;padding:10px 14px;margin:6px 0'>"
            f"<span style='color:#fca5a5;font-weight:700'>🔴 High Risk</span>"
            f"<span style='color:#94a3b8;font-size:0.8rem;margin-left:8px'>"
            f"Supplier has acknowledged / partially delivered. "
            f"They may have already dispatched goods. "
            f"Confirm they have NOT dispatched before cancelling.</span></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div style='background:#1c1107;border:1px solid #f59e0b;"
            f"border-radius:6px;padding:10px 14px;margin:6px 0'>"
            f"<span style='color:#fbbf24;font-weight:700'>⚠️ PO Cancellation</span>"
            f"<span style='color:#94a3b8;font-size:0.8rem;margin-left:8px'>"
            f"PO {po_ref} is SENT to {vendor_name}. "
            f"Cancelling here will NOT automatically notify the supplier.</span></div>",
            unsafe_allow_html=True,
        )

    if not st.session_state.get(f"{_gk}_stage1"):
        reason = st.text_input(
            "Reason for cancellation (required)",
            key=f"{_gk}_reason",
            placeholder="e.g. Customer changed order, stock found locally",
        )
        vendor_confirmed = st.checkbox(
            f"I have confirmed with {vendor_name} that goods are NOT yet dispatched",
            key=f"{_gk}_vendor_check",
        )
        if st.button(
            f"🗑 Cancel PO {po_ref}",
            key=f"{_gk}_btn1",
            type="primary",
            use_container_width=True,
            disabled=not (reason.strip() and vendor_confirmed),
        ):
            st.session_state[f"{_gk}_stage1"] = True
            st.session_state[f"{_gk}_reason_val"] = reason.strip()
            st.rerun()
        return False

    # Confirm stage
    _reason_val = st.session_state.get(f"{_gk}_reason_val", "")
    st.warning(
        f"Final confirm: Cancel PO **{po_ref}** ({vendor_name})? "
        f"Reason: '{_reason_val}'. This cannot be undone."
    )
    _c1, _c2 = st.columns(2)
    with _c1:
        if st.button("✅ Confirm Cancel PO", key=f"{_gk}_confirm",
                     type="primary", use_container_width=True):
            log_override(
                "po_cancel", order_id,
                f"PO {po_ref} status={status}",
                "CANCELLED",
                _reason_val,
                approved_by=current_user_name(),
            )
            # Notify vendor (best-effort)
            _notify_vendor_po_cancel(po_id_int, po_ref, vendor_name, _reason_val)
            st.session_state.pop(f"{_gk}_stage1", None)
            return True
    with _c2:
        if st.button("← Go Back", key=f"{_gk}_back", use_container_width=True):
            st.session_state.pop(f"{_gk}_stage1", None)
            st.rerun()
    return False


def _notify_vendor_po_cancel(po_id_int: int, po_ref: str,
                              vendor_name: str, reason: str) -> None:
    """Send WhatsApp / log when a SENT PO is cancelled."""
    try:
        rows = _q("SELECT vendor_contact FROM supplier_orders WHERE id=%(p)s LIMIT 1",
                  {"p": po_id_int})
        contact = rows[0].get("vendor_contact", "") if rows else ""
        msg = (
            f"🔔 *PO Cancellation*\n"
            f"PO Ref: {po_ref}\n"
            f"Reason: {reason}\n"
            f"Please do not dispatch. Contact us to confirm."
        )
        try:
            from modules.flags.feature_flags import flag
            if flag("enable_whatsapp_po", False) and contact:
                from modules.procurement.po_engine import _send_whatsapp
                _send_whatsapp({"vendor_contact": contact}, msg)
                return
        except Exception:
            pass
        if contact:
            st.info(f"📞 {vendor_name} ({contact}) should be notified. "
                    f"WhatsApp flag is off — notify manually.")
    except Exception:
        pass


__all__ = [
    "check_order_editable_from_retail",
    "render_retail_edit_block",
    "render_price_discount_guard",
    "check_order_deletable",
    "render_delete_order_guard",
    "notify_arc_vendor_on_reversal",
    "render_po_cancel_guard",
]
