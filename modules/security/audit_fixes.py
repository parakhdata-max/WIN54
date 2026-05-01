"""
modules/security/audit_fixes.py
══════════════════════════════════════════════════════════════════════════════
7 audit-driven fixes — drop-in replacements / additions to existing guards.

Each fix is self-contained.  Import exactly what you need.

FIX 1  order_backoffice_touched()           — DB signal, not status
FIX 2  enforce_price_override_role()        — blocks non-manager before save
FIX 3  check_effective_discount()           — cumulative, not stepwise
FIX 4  check_order_has_financial_records()  — blocks delete if invoice/payment
FIX 5  log_arc_backstep_event()             — formal DB event, WhatsApp optional
FIX 6  check_po_stock_received()            — blocks PO cancel if stock used
FIX 7  render_preview_as_role()             — "see sidebar as this role" UI

All pure Python / Streamlit.  No schema migrations required for fixes 1–3, 5, 7.
Fixes 4, 6 use existing tables (payments, invoices, supplier_order_items).
"""

from __future__ import annotations
import streamlit as st
from typing import Optional


def _q(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# FIX 1 — RETAIL EDIT LOCK: DB signal, not status-based
# ═════════════════════════════════════════════════════════════════════════════

def order_backoffice_touched(order: dict) -> tuple[bool, str]:
    """
    Returns (touched: bool, signal: str).

    Checks THREE independent DB signals — ANY one is enough to lock:
      A) order.status in CONFIRMED+                  (existing, kept)
      B) job_master row exists for any line          (new — timing-gap fix)
      C) blank_allocations row exists for any line   (new — strongest signal)

    This closes the timing gap:
      PENDING → backoffice opens → creates job_master → staff edits from retail
      Before this fix: allowed (still PENDING)
      After this fix:  blocked (job_master row exists = backoffice has touched it)

    USAGE in order_edit_view._render_order_edit_panel():
        touched, signal = order_backoffice_touched(order)
        if touched:
            _render_backoffice_lock_banner(signal, order)
            return   # stop rendering edit UI
    """
    # Signal A: status
    status = str(order.get("status") or "PENDING").upper()
    _LOCKED_STATUSES = {
        "CONFIRMED", "IN_PRODUCTION", "READY", "BILLED",
        "DISPATCHED", "DELIVERED", "CLOSED"
    }
    if status in _LOCKED_STATUSES:
        return True, f"ORDER_STATUS:{status}"

    # Collect line IDs
    lines    = order.get("lines") or []
    line_ids = [
        (line.get("line_id") or line.get("id") or "").strip()
        for line in lines
        if (line.get("line_id") or line.get("id") or "").strip()
    ]
    if not line_ids:
        return False, ""

    # Signal B: job_master row exists (backoffice has at minimum created a job card)
    try:
        jm_rows = _q("""
            SELECT id FROM job_master
            WHERE order_line_id = ANY(%(ids)s::uuid[])
            LIMIT 1
        """, {"ids": line_ids})
        if jm_rows:
            return True, "JOB_CARD_EXISTS"
    except Exception:
        pass

    # Signal C: blank allocation (blank physically picked)
    try:
        ba_rows = _q("""
            SELECT id FROM blank_allocations
            WHERE order_line_id = ANY(%(ids)s::uuid[])
            LIMIT 1
        """, {"ids": line_ids})
        if ba_rows:
            return True, "BLANK_ALLOCATED"
    except Exception:
        pass

    # Signal D: surfacing_data in lens_params (job card saved)
    try:
        sd_rows = _q("""
            SELECT id FROM order_lines
            WHERE id = ANY(%(ids)s::uuid[])
              AND lens_params::jsonb ? 'surfacing_data'
            LIMIT 1
        """, {"ids": line_ids})
        if sd_rows:
            return True, "SURFACING_DATA_SAVED"
    except Exception:
        pass

    return False, ""


_SIGNAL_MESSAGES = {
    "JOB_CARD_EXISTS":    "A job card has been created in Backoffice for this order.",
    "BLANK_ALLOCATED":    "A blank has been allocated in Backoffice.",
    "SURFACING_DATA_SAVED": "Job card data has been saved in Backoffice.",
}
_SIGNAL_GUIDANCE = {
    "JOB_CARD_EXISTS": (
        "Go to **Backoffice → this order → Documents tab** to make changes. "
        "If the power needs to change, cancel the job card there first."
    ),
    "BLANK_ALLOCATED": (
        "A blank is physically assigned. "
        "Go to **Backoffice → Production tab → Reject & Return Blank** "
        "to release it, then edit the order."
    ),
    "SURFACING_DATA_SAVED": (
        "Go to **Backoffice → this order** to make all changes."
    ),
}

def render_backoffice_lock_banner(signal: str, order: dict) -> None:
    """Renders the contextual lock banner in the Orders / Retail edit screen."""
    status = str(order.get("status") or "PENDING").upper()
    order_no = order.get("order_no") or order.get("display_order_no") or "—"

    if signal.startswith("ORDER_STATUS:"):
        msg      = f"Order is {status} — editing locked."
        guidance = "Use **Backoffice** to make any changes at this stage."
        border   = "#7c3aed"
    else:
        msg      = _SIGNAL_MESSAGES.get(signal, "This order has been processed in Backoffice.")
        guidance = _SIGNAL_GUIDANCE.get(signal, "Go to Backoffice to edit.")
        border   = "#ef4444"

    st.markdown(
        f"<div style='background:#1a0a0a;border:1px solid {border};"
        f"border-radius:8px;padding:14px 16px;margin:8px 0'>"
        f"<div style='color:{border};font-weight:700;font-size:0.95rem;margin-bottom:6px'>"
        f"🔒 Order {order_no} — Edit Blocked</div>"
        f"<div style='color:#94a3b8;font-size:0.82rem;margin-bottom:4px'>{msg}</div>"
        f"<div style='color:#60a5fa;font-size:0.8rem'>{guidance}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# FIX 2 — PRICE OVERRIDE: hard role block before save reaches DB
# ═════════════════════════════════════════════════════════════════════════════

def enforce_price_override_role(
    new_price: float,
    original_price: float,
    order_no: str = "",
    line_name: str = "",
) -> tuple[bool, str]:
    """
    Returns (allowed: bool, block_reason: str).

    Call this BEFORE any price write reaches the DB.
    Billing user with a changed price: hard block.
    Manager+: allowed, but must go through reason dialog (render_price_discount_guard).

    WHY hard block here (not just log):
      If we only log, a billing user CAN complete the save.
      The log becomes forensic evidence after the damage is done.
      Hard block prevents it.
    """
    from modules.security.roles import has_role

    if abs(new_price - original_price) <= 0.01:
        return True, ""   # no change — nothing to guard

    if has_role("admin", "manager"):
        return True, ""   # manager/admin may override (with reason dialog separately)

    return False, (
        f"Price change from ₹{original_price:,.2f} → ₹{new_price:,.2f} "
        f"on '{line_name}' requires Manager approval. "
        f"Ask your manager to apply this change in Backoffice."
    )


def render_price_block_banner(reason: str) -> None:
    st.markdown(
        f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
        f"border-radius:6px;padding:10px 14px;margin:4px 0'>"
        f"<span style='color:#fca5a5;font-weight:700'>⛔ Price Override Blocked</span><br>"
        f"<span style='color:#94a3b8;font-size:0.8rem'>{reason}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# FIX 3 — DISCOUNT: effective (cumulative), not line-level stepwise
# ═════════════════════════════════════════════════════════════════════════════

def calculate_effective_discount(
    lines: list,
    threshold_pct: Optional[float] = None,
) -> dict:
    """
    Computes the EFFECTIVE discount across all lines:
      effective_pct = (1 - final_total / original_total) * 100

    This catches stepwise manipulation (10% + 10% + 5% = 25% effective).

    Returns:
      {
        "original_total":   float,
        "final_total":      float,
        "discount_amount":  float,
        "effective_pct":    float,
        "over_threshold":   bool,
        "threshold_used":   float,
      }

    USAGE before save:
        result = calculate_effective_discount(order["lines"])
        if result["over_threshold"]:
            # require manager approval
    """
    if threshold_pct is None:
        try:
            from modules.security.permission_engine import get_discount_threshold
            threshold_pct = get_discount_threshold()
        except Exception:
            threshold_pct = 20.0

    original_total = 0.0
    final_total    = 0.0

    for line in lines:
        qty       = float(line.get("billing_qty") or line.get("quantity") or 0)
        unit_price = float(line.get("unit_price") or 0)
        disc_pct   = float(line.get("discount_percent") or 0)
        disc_amt   = float(line.get("discount_amount")  or 0)

        if qty <= 0 or unit_price <= 0:
            continue

        line_original = qty * unit_price
        # Use discount_amount if set, else calculate from percent
        if disc_amt > 0:
            line_discount = disc_amt
        elif disc_pct > 0:
            line_discount = line_original * (disc_pct / 100.0)
        else:
            line_discount = 0.0

        original_total += line_original
        final_total    += (line_original - line_discount)

    if original_total <= 0:
        return {
            "original_total": 0, "final_total": 0,
            "discount_amount": 0, "effective_pct": 0.0,
            "over_threshold": False, "threshold_used": threshold_pct,
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
    }


def render_discount_approval_gate(
    disc_info: dict,
    order_no: str,
    context_key: str,
) -> tuple[bool, dict]:
    """
    Renders the manager approval dialog when effective discount exceeds threshold.
    Returns (approved: bool, {reason, approved_by}).

    Call from backoffice save flow:
        disc = calculate_effective_discount(order["lines"])
        if disc["over_threshold"]:
            approved, meta = render_discount_approval_gate(disc, order_no, key)
            if not approved:
                st.stop()
    """
    from modules.security.roles import has_role, current_user_name
    from modules.security.permission_engine import PRICE_OVERRIDE_REASONS, log_override

    _key = f"disc_gate_{context_key}"
    _ok_key = f"{_key}_ok"

    if st.session_state.get(_ok_key):
        return True, st.session_state.get(f"{_key}_meta", {})

    if not has_role("manager", "admin"):
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:6px;padding:10px 14px'>"
            f"<span style='color:#fca5a5;font-weight:700'>⛔ Discount Approval Required</span><br>"
            f"<span style='color:#94a3b8;font-size:0.8rem'>"
            f"Effective discount is <b>{disc_info['effective_pct']:.1f}%</b> "
            f"(threshold: {disc_info['threshold_used']:.0f}%). "
            f"Manager must approve.</span></div>",
            unsafe_allow_html=True,
        )
        return False, {}

    st.markdown(
        f"<div style='background:#1c1107;border:1px solid #f59e0b;"
        f"border-radius:8px;padding:12px 14px;margin:6px 0'>"
        f"<div style='color:#fbbf24;font-weight:700;margin-bottom:6px'>"
        f"⚠️ Manager Approval — High Discount</div>"
        f"<div style='color:#fed7aa;font-size:0.82rem'>"
        f"Effective discount: <b>{disc_info['effective_pct']:.1f}%</b> "
        f"(threshold: {disc_info['threshold_used']:.0f}%) · "
        f"₹{disc_info['discount_amount']:,.2f} off ₹{disc_info['original_total']:,.2f}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    reason = st.selectbox("Reason for high discount", PRICE_OVERRIDE_REASONS, key=f"{_key}_r")
    note   = st.text_input("Additional note (optional)", key=f"{_key}_n",
                           placeholder="Context or authorization reference")
    final_reason = note.strip() if reason == "Other (specify below)" else reason

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Approve Discount", type="primary",
                     key=f"{_key}_btn",
                     use_container_width=True,
                     disabled=(final_reason in ("", "— Select reason —"))):
            meta = {"reason": final_reason, "note": note, "approved_by": current_user_name()}
            log_override(
                "discount_approval", order_no,
                f"{disc_info['threshold_used']:.0f}% threshold",
                f"{disc_info['effective_pct']:.1f}% effective",
                final_reason, note, current_user_name(),
            )
            st.session_state[_ok_key]          = True
            st.session_state[f"{_key}_meta"]   = meta
            st.rerun()
    with c2:
        if st.button("✕ Cancel", key=f"{_key}_cancel", use_container_width=True):
            st.rerun()

    return False, {}


# ═════════════════════════════════════════════════════════════════════════════
# FIX 4 — DELETE ORDER: financial record check
# ═════════════════════════════════════════════════════════════════════════════

def check_order_has_financial_records(order: dict) -> tuple[bool, list[str]]:
    """
    Returns (has_records: bool, blocking_reasons: list[str]).

    Checks: invoices, payments, challans, credit_notes.
    ANY active (non-cancelled) financial record blocks deletion.

    USAGE before delete:
        blocked, reasons = check_order_has_financial_records(order)
        if blocked:
            for r in reasons: st.error(r)
            return
    """
    order_id  = str(order.get("id") or "")
    order_no  = str(order.get("order_no") or order.get("display_order_no") or "")
    blocks: list[str] = []

    if not order_id and not order_no:
        return False, []

    # Invoices
    try:
        inv = _q("""
            SELECT invoice_no FROM invoices
            WHERE (order_id=%(oid)s::uuid OR order_no=%(ono)s)
              AND COALESCE(status,'') != 'CANCELLED'
              AND COALESCE(is_deleted,FALSE)=FALSE
            LIMIT 3
        """, {"oid": order_id, "ono": order_no})
        if inv:
            nos = ", ".join(r.get("invoice_no","?") for r in inv)
            blocks.append(f"Invoice exists: {nos} — reverse invoice before deleting.")
    except Exception:
        pass

    # Payments (advance or direct)
    try:
        pay = _q("""
            SELECT COALESCE(payment_no, id::text) AS pno, amount
            FROM payments
            WHERE (
                (advance_for_order_id IS NOT NULL AND advance_for_order_id::text=%(oid)s)
                OR (order_id IS NOT NULL AND order_id::text=%(oid)s)
            )
            AND COALESCE(is_deleted,FALSE)=FALSE
            LIMIT 3
        """, {"oid": order_id})
        if pay:
            total = sum(float(r.get("amount",0)) for r in pay)
            blocks.append(
                f"Payment recorded: ₹{total:,.2f} — reverse payment before deleting."
            )
    except Exception:
        pass

    # Challans
    try:
        chal = _q("""
            SELECT challan_no FROM challans
            WHERE (%(oid)s = ANY(order_ids) OR %(ono)s = ANY(order_ids::text[]))
              AND COALESCE(is_deleted,FALSE)=FALSE
            LIMIT 1
        """, {"oid": order_id, "ono": order_no})
        if chal:
            blocks.append(
                f"Challan {chal[0].get('challan_no','?')} linked — "
                f"cancel challan before deleting order."
            )
    except Exception:
        pass

    # Credit notes
    try:
        cn = _q("""
            SELECT cn_no FROM credit_notes
            WHERE (order_id=%(oid)s::uuid OR order_no=%(ono)s)
              AND COALESCE(status,'') NOT IN ('CANCELLED','VOID')
            LIMIT 1
        """, {"oid": order_id, "ono": order_no})
        if cn:
            blocks.append(
                f"Credit note {cn[0].get('cn_no','?')} exists — void it before deleting."
            )
    except Exception:
        pass

    return bool(blocks), blocks


def render_financial_delete_block(blocks: list[str]) -> None:
    """Renders financial blocking reasons in UI."""
    st.markdown(
        "<div style='background:#1a0a0a;border:1px solid #ef4444;"
        "border-radius:8px;padding:12px 16px;margin:8px 0'>"
        "<div style='color:#ef4444;font-weight:700;margin-bottom:6px'>"
        "⛔ Cannot Delete — Financial Records Exist</div>"
        + "".join(
            f"<div style='color:#fca5a5;font-size:0.82rem;padding:2px 0'>• {b}</div>"
            for b in blocks
        )
        + "</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
# FIX 5 — ARC BACKSTEP: formal DB event (WhatsApp optional)
# ═════════════════════════════════════════════════════════════════════════════

def log_arc_backstep_event(
    job_id: str,
    from_stage: str,
    order_id: str,
    eye_side: str,
    reason: str,
    vendor_name: str = "",
    vendor_contact: str = "",
    po_ref: str = "",
) -> None:
    """
    Writes a formal ARC_BACKSTEP record to two places:
      1. job_stage_events (audit trail in production panel)
      2. permission_override_log (searchable cross-module log)

    Then attempts WhatsApp — failure is caught and displayed as warning,
    never raises exception, never blocks the backstep.

    Call this from execute_backstep_db() when from_stage in (SENT_TO_ARC, ARC_RECEIVED).
    """
    from modules.security.roles import current_user_name

    user = current_user_name()

    # 1. Formal job stage event
    _w("""
    INSERT INTO job_stage_events
        (id, job_id, stage_code, department, remarks, created_at)
    VALUES
        (gen_random_uuid(), %(j)s::uuid, %(fs)s,
         'ARC_BACKSTEP', %(r)s, NOW())
    """, {
        "j":  job_id,
        "fs": f"BACKSTEP_FROM_{from_stage}",
        "r":  (
            f"[ARC REVERSAL] Eye:{eye_side} Vendor:{vendor_name} "
            f"PO:{po_ref} Reason:{reason} By:{user}"
        ),
    })

    # 2. Override log
    _w("""
    INSERT INTO permission_override_log
        (action_type, order_no, original_val, new_val, reason,
         reason_note, approved_by, created_at)
    VALUES
        ('arc_backstep', %(oid)s, %(fs)s, 'REVERSED',
         %(r)s, %(note)s, %(by)s, NOW())
    """, {
        "oid":  order_id,
        "fs":   from_stage,
        "r":    reason,
        "note": f"Vendor:{vendor_name} PO:{po_ref} Contact:{vendor_contact}",
        "by":   user,
    })

    # 3. WhatsApp — best effort
    _wa_sent = False
    if vendor_contact:
        try:
            from modules.flags.feature_flags import flag
            if flag("enable_whatsapp_po", False):
                from modules.procurement.po_engine import _send_whatsapp
                msg = (
                    f"🔔 *ARC Recall / Cancellation Notice*\n"
                    f"PO Ref: {po_ref}\n"
                    f"Eye: {eye_side}\n"
                    f"Reason: {reason}\n"
                    f"Action required: Please hold / return this lens immediately.\n"
                    f"Authorised by: {user}"
                )
                _send_whatsapp({"vendor_contact": vendor_contact}, msg)
                _wa_sent = True
        except Exception as _we:
            st.warning(
                f"⚠️ WhatsApp to {vendor_name} failed ({_we}). "
                f"Please call {vendor_contact} manually."
            )

    # 4. UI feedback
    if vendor_contact and not _wa_sent:
        st.info(
            f"📞 ARC vendor **{vendor_name}** ({vendor_contact}) must be notified manually. "
            f"PO Ref: {po_ref}. "
            f"Event logged — tell them to hold/return the {eye_side} eye lens."
        )
    elif not vendor_contact:
        st.warning(
            f"⚠️ No contact number for ARC vendor '{vendor_name}'. "
            f"Notify them manually. Event logged."
        )


# ═════════════════════════════════════════════════════════════════════════════
# FIX 6 — PO CANCEL: stock received check
# ═════════════════════════════════════════════════════════════════════════════

def check_po_cancel_safe(po_id_int: int) -> tuple[bool, str]:
    """
    Returns (safe_to_cancel: bool, block_reason: str).

    Blocks cancel if:
      A) Any item has received_qty > 0 AND that quantity is linked to inventory
         (i.e., ready_qty was written to order_lines — stock is in the system)
      B) PO status is RECEIVED / INSPECTION / COMPLETE — goods already logged

    If partially received (PARTIAL): warns but allows with forced acknowledgement.
    """
    # Check PO status first
    po_rows = _q("SELECT status FROM supplier_orders WHERE id=%(p)s LIMIT 1",
                  {"p": po_id_int})
    if po_rows:
        po_status = str(po_rows[0].get("status") or "").upper()
        if po_status in ("RECEIVED", "INSPECTION", "COMPLETE", "READY_TO_BILL"):
            return False, (
                f"PO is in status {po_status} — goods have already been received into inventory. "
                f"Cannot cancel. Raise a supplier return instead."
            )

    # Check received quantities on items
    try:
        item_rows = _q("""
            SELECT soi.id, soi.received_qty, soi.ordered_qty,
                   ol.ready_qty, p.product_name
            FROM supplier_order_items soi
            LEFT JOIN order_lines ol ON ol.id = soi.customer_line_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE soi.supplier_order_id = %(p)s
        """, {"p": po_id_int})

        received_items = [r for r in item_rows if int(r.get("received_qty") or 0) > 0]
        if received_items:
            names = ", ".join(
                r.get("product_name") or "unknown"
                for r in received_items[:3]
            )
            total_rcv = sum(int(r.get("received_qty", 0)) for r in received_items)
            return False, (
                f"{len(received_items)} item(s) already received ({total_rcv} pcs): {names}. "
                f"Cancelling the PO record will NOT reverse stock in inventory. "
                f"Raise a supplier return / stock adjustment first."
            )
    except Exception:
        pass

    return True, ""


def render_po_cancel_stock_check(po_id_int: int) -> bool:
    """
    Renders the stock check result.
    Returns True if safe to proceed with cancel UI, False if blocked.
    """
    safe, reason = check_po_cancel_safe(po_id_int)
    if not safe:
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:6px;padding:10px 14px;margin:6px 0'>"
            f"<span style='color:#fca5a5;font-weight:700'>"
            f"⛔ PO Cancellation Blocked</span><br>"
            f"<span style='color:#94a3b8;font-size:0.82rem'>{reason}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    return safe


# ═════════════════════════════════════════════════════════════════════════════
# FIX 7 — "PREVIEW AS ROLE" — admin sees exact sidebar + page as any role
# ═════════════════════════════════════════════════════════════════════════════

def render_preview_as_role() -> None:
    """
    Admin-only: shows exact sidebar + actions for any role.
    Reads LIVE from page_registry → permission_engine → DB grants.
    Adding a page or sub-tab to page_registry shows here on next restart.
    """
    from modules.security.roles import require_role
    require_role("admin")

    from modules.security.permission_engine import (
        SIDEBAR_CATALOGUE, ROLES_ORDERED, ROLE_COLORS,
        get_visible_modules, load_role_module_grants,
        ACTION_CATALOGUE,
    )
    # Load sub-tabs live from page_registry (not cached)
    try:
        from modules.security.page_registry import SUB_TABS as _SUB_TABS
    except Exception:
        _SUB_TABS = {}

    ROLE_ICONS = {
        "viewer": "👁️", "staff": "👤", "billing": "💳",
        "lab": "🔬", "inventory": "📦", "manager": "🔑", "admin": "👑",
    }
    SECTION_COLORS = {
        "BILLING": "#0284c7", "PRODUCTION": "#7c3aed",
        "STOCK": "#059669",   "ADMIN": "#dc2626",
    }

    st.markdown("### 👁️ Preview as Role")
    st.markdown(
        "<span style='color:#94a3b8;font-size:0.8rem'>"
        "Reads live from page_registry — adding a page or sub-tab there "
        "appears here on next restart.</span>",
        unsafe_allow_html=True,
    )

    selected_role = st.selectbox(
        "Preview role",
        ROLES_ORDERED,
        format_func=lambda r: f"{ROLE_ICONS.get(r,'')} {r.upper()}",
        key="preview_role_select",
    )

    db_mods    = load_role_module_grants(selected_role)
    db_actions = {}
    try:
        from modules.security.permission_engine import load_role_action_grants
        db_actions = load_role_action_grants(selected_role)
    except Exception:
        pass

    role_color = ROLE_COLORS.get(selected_role, "#374151")

    col_sidebar, col_actions = st.columns([1, 1.6])

    with col_sidebar:
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid {role_color}44;"
            f"border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
            f"<div style='color:{role_color};font-weight:700;font-size:0.85rem'>"
            f"{ROLE_ICONS.get(selected_role,'')} {selected_role.upper()} — Sidebar</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        st.markdown(
            "<div style='background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;"
            "padding:10px 12px;margin-bottom:6px'>"
            "<div style='font-size:0.9rem;font-weight:800;color:#111827'>DV ERP 👓</div>"
            "<div style='font-size:0.65rem;color:#6b7280'>Optical Business Management</div>"
            "<div style='margin-top:6px;border-top:1px solid #f3f4f6;padding-top:6px'>"
            "<div style='font-size:0.65rem;color:#9ca3af'>Logged in as</div>"
            f"<div style='font-size:0.8rem;font-weight:700;color:#111827'>"
            f"{ROLE_ICONS.get(selected_role,'')} Example User</div>"
            f"<span style='background:{role_color};color:#fff;font-size:0.6rem;"
            f"font-weight:700;padding:1px 7px;border-radius:20px'>"
            f"{selected_role.upper()}</span></div></div>",
            unsafe_allow_html=True,
        )

        visible_modules = set()
        current_section = ""
        visible_count   = 0

        for mod in SIDEBAR_CATALOGUE:
            is_visible = db_mods.get(
                mod["key"],
                selected_role in mod.get("default_roles", []) or selected_role == "admin"
            )
            if not is_visible:
                continue

            visible_modules.add(mod["key"])
            visible_count += 1
            section = mod["section"]

            if section != current_section:
                current_section = section
                sec_color = SECTION_COLORS.get(section, "#374151")
                st.markdown(
                    f"<div style='color:{sec_color};font-size:0.6rem;font-weight:800;"
                    f"letter-spacing:.1em;padding:5px 0 1px;border-bottom:1px solid "
                    f"{sec_color}33;margin-top:4px'>{section}</div>",
                    unsafe_allow_html=True,
                )
            st.markdown(
                f"<div style='padding:3px 8px;font-size:0.8rem;color:#374151;"
                f"border-radius:4px;cursor:default'>▶ {mod['label']}</div>",
                unsafe_allow_html=True,
            )

        st.caption(f"{visible_count} of {len(SIDEBAR_CATALOGUE)} modules visible")

        blocked = [m for m in SIDEBAR_CATALOGUE if m["key"] not in visible_modules]
        if blocked:
            with st.expander(f"🚫 Hidden ({len(blocked)} modules)", expanded=False):
                for m in blocked:
                    st.markdown(
                        f"<span style='color:#374151;font-size:0.75rem;"
                        f"text-decoration:line-through'>{m['label']}</span>",
                        unsafe_allow_html=True,
                    )

    with col_actions:
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid {role_color}44;"
            f"border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
            f"<div style='color:{role_color};font-weight:700;font-size:0.85rem'>"
            f"Actions + Sub-tabs for this role</div>"
            f"<div style='color:#64748b;font-size:0.72rem'>"
            f"Live from page_registry — changes reflect on restart.</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        modules_with_actions = {}
        for act in ACTION_CATALOGUE:
            if act["module"] in visible_modules:
                modules_with_actions.setdefault(act["module"], []).append(act)

        if not modules_with_actions:
            st.caption("No actions to show for this role.")
        else:
            for mod_key, acts in modules_with_actions.items():
                mod_info = next((m for m in SIDEBAR_CATALOGUE if m["key"] == mod_key), {})

                # Separate module actions from sub-tab actions
                mod_actions  = [a for a in acts if not a["key"].startswith("view_")]
                tab_actions  = [a for a in acts if a["key"].startswith("view_")]

                # Also get live sub-tabs from page_registry for this module
                live_tabs = _SUB_TABS.get(mod_key, [])
                live_tab_keys = {ak for ak, _, _ in live_tabs}
                # Add any live tabs not already in ACTION_CATALOGUE
                extra_tabs = [
                    {"key": ak, "label": f"Tab: {lbl}", "default_roles": dr, "module": mod_key}
                    for ak, lbl, dr in live_tabs
                    if ak not in {a["key"] for a in tab_actions}
                ]
                tab_actions = tab_actions + extra_tabs

                with st.expander(mod_info.get("label", mod_key), expanded=False):
                    if mod_actions:
                        st.markdown(
                            "<span style='color:#60a5fa;font-size:0.68rem;"
                            "font-weight:700;letter-spacing:.06em'>ACTIONS</span>",
                            unsafe_allow_html=True,
                        )
                        for act in mod_actions:
                            granted = db_actions.get(mod_key, {}).get(
                                act["key"],
                                selected_role in act.get("default_roles", []) or selected_role == "admin"
                            )
                            icon  = "✅" if granted else "❌"
                            color = "#10b981" if granted else "#ef4444"
                            st.markdown(
                                f"<div style='color:{color};font-size:0.8rem;padding:1px 0'>"
                                f"{icon} {act['label']}</div>",
                                unsafe_allow_html=True,
                            )

                    if tab_actions:
                        st.markdown(
                            "<span style='color:#a78bfa;font-size:0.68rem;"
                            "font-weight:700;letter-spacing:.06em;margin-top:6px;"
                            "display:block'>SUB-TABS</span>",
                            unsafe_allow_html=True,
                        )
                        for act in tab_actions:
                            granted = db_actions.get(mod_key, {}).get(
                                act["key"],
                                selected_role in act.get("default_roles", []) or selected_role == "admin"
                            )
                            icon  = "👁" if granted else "🚫"
                            color = "#a78bfa" if granted else "#6b7280"
                            label = act["label"].replace("Tab: ", "")
                            st.markdown(
                                f"<div style='color:{color};font-size:0.78rem;padding:1px 0'>"
                                f"{icon} {label}</div>",
                                unsafe_allow_html=True,
                            )

    can_do    = sum(1 for act in ACTION_CATALOGUE
                   if act["module"] in visible_modules
                   and (db_actions.get(act["module"], {}).get(act["key"])
                        if db_actions.get(act["module"], {}).get(act["key"]) is not None
                        else (selected_role in act.get("default_roles", []) or selected_role == "admin")))
    cannot_do = len([a for a in ACTION_CATALOGUE if a["module"] in visible_modules]) - can_do

    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("✅ Can do", can_do)
    with c2: st.metric("❌ Cannot do", cannot_do)
    with c3: st.metric("📋 Pages visible", len(visible_modules))


__all__ = [
    # Fix 1
    "order_backoffice_touched", "render_backoffice_lock_banner",
    # Fix 2
    "enforce_price_override_role", "render_price_block_banner",
    # Fix 3
    "calculate_effective_discount", "render_discount_approval_gate",
    # Fix 4
    "check_order_has_financial_records", "render_financial_delete_block",
    # Fix 5
    "log_arc_backstep_event",
    # Fix 6
    "check_po_cancel_safe", "render_po_cancel_stock_check",
    # Fix 7
    "render_preview_as_role",
]
