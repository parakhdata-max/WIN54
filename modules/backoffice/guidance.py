"""
modules/backoffice/guidance.py
================================
Single source of truth for all stage-wise guidance messages.

Usage anywhere in the codebase:
    from modules.backoffice.guidance import get_stage_guidance, render_stage_guidance

No DB calls. No Streamlit state. Pure reference data.
"""
from __future__ import annotations
from typing import Optional
import streamlit as st


def _actor_name() -> str:
    """Real logged-in user for status-history attribution.

    Was hardcoded "backoffice" in hold/resume/cancel — that defeated the
    audit trail (every action looked like it came from a generic account).
    Now records the actual operator. Fail-safe: if identity is unavailable
    for any reason, falls back to "backoffice" so behaviour never regresses.
    """
    try:
        from modules.security.roles import current_user_name
        return current_user_name() or "backoffice"
    except Exception:
        return "backoffice"


# ── STAGE GUIDANCE REGISTRY ──────────────────────────────────────────────────
# Keys are exact stage codes stored in job_master.current_stage / order.status.
# Each entry: title, what_to_do, what_NOT_to_do, who_does_it

STAGE_GUIDANCE: dict[str, dict] = {

    # ── Order status stages ───────────────────────────────────────────────────
    "PENDING": {
        "title":    "📋 Order Received — Pending Confirmation",
        "do":       "Review Rx, check stock availability, confirm with party if wholesale.",
        "dont":     "Do not create job cards yet. Do not allocate stock until confirmed.",
        "who":      "Front desk / backoffice manager",
        "next":     "CONFIRMED",
    },
    "UNDER_REVIEW": {
        "title":    "🔍 Under Review",
        "do":       "Check power range, product availability, or credit limit issue.",
        "dont":     "Do not advance or bill until review is resolved.",
        "who":      "Supervisor / manager",
        "next":     "CONFIRMED or CANCELLED",
    },
    "CONFIRMED": {
        "title":    "✅ Order Confirmed",
        "do":       "Assign manufacturing route (inhouse / supplier / stock) for each line. Print job cards for inhouse lines.",
        "dont":     "Do not edit powers after confirmation without supervisor override.",
        "who":      "Backoffice / assignment team",
        "next":     "IN_PRODUCTION",
    },
    "IN_PRODUCTION": {
        "title":    "⚙️ In Production",
        "do":       "Track job card stages. Follow up with supplier if lab order. Check daily.",
        "dont":     "Do not edit product or pricing. Do not create challan until READY_TO_BILL.",
        "who":      "Production supervisor",
        "next":     "READY (all jobs complete)",
    },
    "READY": {
        "title":    "📦 Ready for Billing",
        "do":       "All jobs complete. Create challan from Billing Summary tab.",
        "dont":     "Do not dispatch before challan is created.",
        "who":      "Billing / dispatch team",
        "next":     "CHALLANED",
    },
    "CHALLANED": {
        "title":    "🧾 Challan Created",
        "do":       "Collect payment (retail: before invoice). Convert to invoice after payment confirmed.",
        "dont":     "Do not convert to invoice without payment confirmation for retail orders.",
        "who":      "Billing / accounts team",
        "next":     "BILLED",
    },
    "BILLED": {
        "title":    "💰 Invoice Raised",
        "do":       "Dispatch goods. Record payment if not already collected.",
        "dont":     "Do not re-open or re-bill without cancelling the existing invoice first.",
        "who":      "Dispatch / accounts",
        "next":     "DISPATCHED",
    },
    "DISPATCHED": {
        "title":    "🚚 Dispatched",
        "do":       "Share tracking details with customer / party. Mark DELIVERED on receipt.",
        "dont":     "Do not issue a replacement or credit note without manager approval.",
        "who":      "Dispatch team",
        "next":     "DELIVERED",
    },
    "DELIVERED": {
        "title":    "✅ Delivered",
        "do":       "Confirm delivery with customer. Collect outstanding balance if any.",
        "dont":     "No further action unless a return / exchange is raised.",
        "who":      "Front desk / accounts",
        "next":     "CLOSED",
    },
    "CLOSED": {
        "title":    "🔒 Order Closed",
        "do":       "Order is complete. No further action.",
        "dont":     "Do not reopen without supervisor approval.",
        "who":      "System / supervisor",
        "next":     None,
    },
    "HOLD": {
        "title":    "⏸ Order On Hold",
        "do":       "Resolve the hold reason before resuming. Document resolution note.",
        "dont":     "Do NOT advance any production stage, create challan, or dispatch while on hold.",
        "who":      "Manager / supervisor",
        "next":     "Previous status (resume) or CANCELLED",
        "warning":  True,
    },
    "CANCELLED": {
        "title":    "🚫 Order Cancelled",
        "do":       "Reverse any stock allocation. Cancel open supplier orders if not yet dispatched. Notify party.",
        "dont":     "Do not create any new billing documents for this order.",
        "who":      "Manager",
        "next":     None,
        "warning":  True,
    },

    # ── Inhouse job stages ────────────────────────────────────────────────────
    "JOB_CREATED": {
        "title":    "🔧 Job Card Created",
        "do":       "Print job card. Assign blank lens from replenishment stock.",
        "dont":     "Do not start surfacing without assigning a blank.",
        "who":      "Production / surfacing team",
        "next":     "PRINTED",
    },
    "PRINTED": {
        "title":    "🖨 Job Card Printed",
        "do":       "Hand job card to lab / production team. Blank must be assigned.",
        "dont":     "Do not skip blank assignment.",
        "who":      "Lab incharge",
        "next":     "PRODUCTION_PICKED",
    },
    "PRODUCTION_PICKED": {
        "title":    "⚙️ Picked for Production",
        "do":       "Lens is with the surfacing machine operator. Track throughput time.",
        "dont":     "Do not return to stock once picked unless rejected.",
        "who":      "Machine operator",
        "next":     "PRODUCTION_DONE",
    },
    "PRODUCTION_DONE": {
        "title":    "✨ Production Complete",
        "do":       "Move to first inspection. Check optical values against Rx.",
        "dont":     "Do not skip inspection.",
        "who":      "QC / senior optician",
        "next":     "INSPECTION",
    },
    "INSPECTION": {
        "title":    "🔍 Inspection",
        "do":       "Verify SPH / CYL / AXIS / ADD within tolerance. Check for scratches.",
        "dont":     "Do not pass if power deviation exceeds ±0.12D sphere or ±0.09D cylinder.",
        "who":      "QC team",
        "next":     "Coating stage or READY_FOR_PACK",
    },
    "HARDCOAT_PICKED": {
        "title":    "🛡 Sent for Hardcoat",
        "do":       "Log dispatch time. Expected return within agreed SLA.",
        "dont":     "Do not advance without receiving confirmation from hardcoat vendor.",
        "who":      "Dispatch / coating team",
        "next":     "HARDCOAT_DONE",
    },
    "HARDCOAT_DONE": {
        "title":    "🛡 Hardcoat Complete",
        "do":       "Inspect for bubbles, peel, or haze. Re-inspect optical values.",
        "dont":     "Do not send for ARC if hardcoat has defects — reject and redo.",
        "who":      "QC team",
        "next":     "ARC_SENT or INSPECTION",
    },
    "ARC_SENT": {
        "title":    "🔬 Sent for ARC / GMC",
        "do":       "Track ARC vendor SLA. Typical 1–2 days.",
        "dont":     "Do not mark received until physically in hand.",
        "who":      "Dispatch team",
        "next":     "ARC_RECEIVED",
    },
    "ARC_RECEIVED": {
        "title":    "🔬 ARC Received",
        "do":       "Inspect coating for uniformity, reflection colour, no delamination.",
        "dont":     "Do not skip final QC.",
        "who":      "QC team",
        "next":     "FINAL_QC",
    },
    "FINAL_QC": {
        "title":    "🔍 Final QC",
        "do":       "Final optical check. Approve or reject. Confirm coating matches order.",
        "dont":     "Do not release to pack with any visible defect.",
        "who":      "Senior optician / QC head",
        "next":     "READY_FOR_PACK",
    },
    "COLOURING_PICKED": {
        "title":    "🎨 Sent for Colouring",
        "do":       "Specify shade / density clearly. Attach colour sample if possible.",
        "dont":     "Do not accept back without colour confirmation from customer.",
        "who":      "Colouring vendor / dispatch",
        "next":     "COLOURING_DONE",
    },
    "COLOURING_DONE": {
        "title":    "🎨 Colouring Complete",
        "do":       "Check shade against customer requirement. Re-inspect power.",
        "dont":     "Do not proceed to coating if colour is wrong — verify with customer first.",
        "who":      "QC / front desk",
        "next":     "INSPECTION or HARDCOAT_PICKED",
    },
    "FITTING_PENDING": {
        "title":    "🔧 Fitting Pending",
        "do":       "Hand lens and frame to fitting team / external fitter. Print fitting slip.",
        "dont":     "Do not dispatch without fitting confirmation.",
        "who":      "Fitting team",
        "next":     "FITTING_SENT",
    },
    "FITTING_SENT": {
        "title":    "🔧 Sent for Fitting",
        "do":       "Track with external fitter. Confirm expected return date.",
        "dont":     "Do not mark received until physically back.",
        "who":      "Dispatch / fitting team",
        "next":     "FITTING_RECEIVED",
    },
    "FITTING_RECEIVED": {
        "title":    "🔧 Fitting Received",
        "do":       "Check fit quality, PD centration, axis alignment.",
        "dont":     "Do not hand to customer without fit verification.",
        "who":      "Dispensing optician",
        "next":     "FITTING_DONE",
    },
    "FITTING_DONE": {
        "title":    "✅ Fitting Complete",
        "do":       "Spectacles ready. Move to packing.",
        "dont":     "Do not bill before fitting is signed off.",
        "who":      "Dispensing optician",
        "next":     "READY_FOR_PACK",
    },
    "READY_FOR_PACK": {
        "title":    "📦 Ready for Packing",
        "do":       "Pack spectacles in case with cleaning cloth. Attach label.",
        "dont":     "Do NOT create challan at this stage — advance to Ready to Bill first.",
        "who":      "Packing team",
        "next":     "READY_TO_BILL",
        "warning":  True,  # billing not allowed here
    },
    "READY_TO_BILL": {
        "title":    "💰 Ready to Bill",
        "do":       "Create challan from Billing Summary. Collect payment (retail: before invoice).",
        "dont":     "Do not dispatch before challan is created.",
        "who":      "Billing team",
        "next":     "CHALLANED",
    },
    "REJECTED": {
        "title":    "🚫 Rejected",
        "do":       "Document rejection reason. Notify customer. Decide: redo, replace, or cancel.",
        "dont":     "Do not discard the rejected lens without supervisor sign-off.",
        "who":      "QC / supervisor",
        "next":     "PRODUCTION_PICKED (redo) or order CANCELLED",
        "warning":  True,
    },
}

# ── HOLD / CANCEL reason dropdown options ─────────────────────────────────────
HOLD_REASONS = [
    "— Select reason —",
    "Issue with order",
    "Technical issue",
    "Payment pending",
    "Power change requested",
    "Material not available",
    "Others",
]

CANCEL_REASONS = [
    "— Select reason —",
    "Issue with order",
    "Technical issue",
    "Customer request",
    "Duplicate order",
    "Product not available",
    "Others",
]

# ── Public API ────────────────────────────────────────────────────────────────

def get_stage_guidance(stage_code: str) -> Optional[dict]:
    """Return guidance dict for a stage code, or None if not found."""
    return STAGE_GUIDANCE.get(str(stage_code or "").upper().strip())


def render_stage_guidance(stage_code: str, compact: bool = False) -> None:
    """
    Render a guidance card for the given stage inline in Streamlit.
    compact=True → small caption-style, compact=False → full expander card.
    """
    g = get_stage_guidance(stage_code)
    if not g:
        return

    is_warn = g.get("warning", False)
    border_color = "#f97316" if is_warn else "#3b82f6"
    bg_color     = "#1a0800" if is_warn else "#0f172a"

    if compact:
        st.markdown(
            f"<div style='border-left:3px solid {border_color};"
            f"background:{bg_color};padding:4px 10px;border-radius:4px;"
            f"font-size:0.75rem;color:#94a3b8;margin:4px 0'>"
            f"<b style='color:{border_color}'>{g['title']}</b> — {g['do']}"
            f"</div>",
            unsafe_allow_html=True,
        )
        return

    with st.expander(f"📖 Stage Guide: {g['title']}", expanded=False):
        c1, c2 = st.columns(2)
        c1.markdown(f"**✅ What to do**\n\n{g['do']}")
        c2.markdown(f"**🚫 Do NOT**\n\n{g['dont']}")
        st.caption(f"👤 Responsibility: **{g['who']}**")
        if g.get("next"):
            st.caption(f"➡️ Next stage: **{g['next']}**")


def render_hold_confirm_panel(order_id: str, order_no: str,
                               current_status: str) -> bool:
    """
    Render Hold confirmation panel.
    Returns True if Hold was confirmed and saved.
    """
    key_pfx = f"hold_{order_id[:8]}"

    if current_status.upper() == "HOLD":
        # Already on hold — show resume option
        st.markdown(
            "<div style='background:#1a0a00;border:2px solid #f97316;"
            "border-radius:8px;padding:12px 16px;margin:8px 0'>"
            "<span style='color:#fb923c;font-size:0.9rem;font-weight:700'>"
            "⏸ This order is currently ON HOLD</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption("Resolve the hold reason before resuming production.")
        if st.button("▶️ Resume — Set to Normal (CONFIRMED)",
                     key=f"{key_pfx}_resume",
                     use_container_width=True,
                     type="primary"):
            try:
                from modules.sql_adapter import run_write, run_query
                # Restore to previous status from history, default CONFIRMED.
                # Canonical schema columns are from_status/to_status — NOT
                # prev_status/new_status (which do not exist in this DB).
                _hist = run_query(
                    """SELECT from_status FROM order_status_history
                       WHERE order_id = %(oid)s::uuid AND to_status = 'HOLD'
                       ORDER BY changed_at DESC LIMIT 1""",
                    {"oid": order_id},
                )
                _prev = (_hist[0]["from_status"] if _hist else None) or "CONFIRMED"
                _resume_to = _prev if _prev not in ("HOLD","CANCELLED") else "CONFIRMED"
                run_write(
                    """UPDATE orders SET status=%(s)s, updated_at=NOW()
                       WHERE id=%(oid)s::uuid""",
                    {"s": _resume_to, "oid": order_id},
                )
                run_write(
                    """INSERT INTO order_status_history
                       (order_id, from_status, to_status, changed_by_name,
                        remarks, changed_at)
                       VALUES (%(oid)s::uuid, 'HOLD', %(s)s, %(by)s,
                               'Hold resolved — resumed', NOW())""",
                    {"oid": order_id,
                     "s": _resume_to, "by": _actor_name()},
                )
                st.success(f"✅ Order resumed — status set to {_resume_to}")
                import time; time.sleep(0.3)
                st.rerun()
            except Exception as _e:
                st.error(f"Resume failed: {_e}")
        return False

    # ── Hold confirmation flow ────────────────────────────────────────────────
    _confirm_key = f"{key_pfx}_confirm_shown"
    if not st.session_state.get(_confirm_key):
        if st.button("⏸ Put On Hold",
                     key=f"{key_pfx}_btn",
                     use_container_width=True):
            st.session_state[_confirm_key] = True
            st.rerun()
        render_stage_guidance("HOLD", compact=True)
        return False

    # Confirmation panel
    st.warning(
        "⚠️ **Confirm Hold** — All production, billing and dispatch actions "
        f"will be blocked for order **{order_no}** until hold is removed."
    )
    _reason = st.selectbox(
        "Reason for hold *",
        HOLD_REASONS,
        key=f"{key_pfx}_reason",
    )
    _note = st.text_input(
        "Additional note (optional)",
        key=f"{key_pfx}_note",
        placeholder="e.g. Customer requested colour change",
    )
    _hc1, _hc2 = st.columns(2)
    with _hc1:
        _do_hold = st.button("✅ Confirm Hold",
                             key=f"{key_pfx}_do",
                             type="primary",
                             use_container_width=True,
                             disabled=(_reason == HOLD_REASONS[0]),
                             help="Select a reason first")
    with _hc2:
        if st.button("✗ Cancel",
                     key=f"{key_pfx}_abort",
                     use_container_width=True):
            st.session_state.pop(_confirm_key, None)
            st.rerun()

    if _do_hold and _reason != HOLD_REASONS[0]:
        try:
            from modules.sql_adapter import run_write
            run_write(
                """UPDATE orders SET status='HOLD', updated_at=NOW()
                   WHERE id=%(oid)s::uuid""",
                {"oid": order_id},
            )
            run_write(
                """INSERT INTO order_status_history
                   (order_id, from_status, to_status, changed_by_name,
                    remarks, changed_at)
                   VALUES (%(oid)s::uuid, %(prev)s, 'HOLD',
                           %(by)s, %(reason)s, NOW())""",
                {"oid": order_id,
                 "prev": current_status,
                 "reason": _reason + (f" — {_note}" if _note.strip() else ""),
                 "by": _actor_name()},
            )
            st.session_state.pop(_confirm_key, None)
            st.success(f"✅ Order {order_no} placed on Hold.")
            import time; time.sleep(0.3)
            st.rerun()
            return True
        except Exception as _e:
            st.error(f"Hold failed: {_e}")

    return False


def render_cancel_confirm_panel(order_id: str, order_no: str,
                                current_status: str) -> bool:
    """
    Render Cancel confirmation panel.
    Returns True if Cancel was confirmed and saved.
    """
    key_pfx = f"cancel_{order_id[:8]}"

    if current_status.upper() == "CANCELLED":
        st.info("This order is already cancelled.")
        return False

    _confirm_key = f"{key_pfx}_confirm_shown"
    if not st.session_state.get(_confirm_key):
        if st.button("🚫 Cancel Order",
                     key=f"{key_pfx}_btn",
                     use_container_width=True):
            st.session_state[_confirm_key] = True
            st.rerun()
        render_stage_guidance("CANCELLED", compact=True)
        return False

    st.error(
        "🚫 **Confirm Cancellation** — This will permanently cancel order "
        f"**{order_no}**. Ensure all supplier orders and stock allocations "
        "are reversed first."
    )
    _reason = st.selectbox(
        "Reason for cancellation *",
        CANCEL_REASONS,
        key=f"{key_pfx}_reason",
    )
    _note = st.text_input(
        "Additional note (optional)",
        key=f"{key_pfx}_note",
        placeholder="e.g. Customer changed mind, product unavailable",
    )
    _gc1, _gc2 = st.columns(2)
    with _gc1:
        _do_cancel = st.button("✅ Confirm Cancel",
                               key=f"{key_pfx}_do",
                               type="primary",
                               use_container_width=True,
                               disabled=(_reason == CANCEL_REASONS[0]),
                               help="Select a reason first")
    with _gc2:
        if st.button("✗ Keep Order",
                     key=f"{key_pfx}_abort",
                     use_container_width=True):
            st.session_state.pop(_confirm_key, None)
            st.rerun()

    if _do_cancel and _reason != CANCEL_REASONS[0]:
        try:
            from modules.sql_adapter import run_write
            run_write(
                """UPDATE orders SET status='CANCELLED', updated_at=NOW()
                   WHERE id=%(oid)s::uuid""",
                {"oid": order_id},
            )
            run_write(
                """INSERT INTO order_status_history
                   (order_id, from_status, to_status, changed_by_name,
                    remarks, changed_at)
                   VALUES (%(oid)s::uuid, %(prev)s, 'CANCELLED',
                           %(by)s, %(reason)s, NOW())""",
                {"oid": order_id,
                 "prev": current_status,
                 "reason": _reason + (f" — {_note}" if _note.strip() else ""),
                 "by": _actor_name()},
            )
            st.session_state.pop(_confirm_key, None)
            st.success(f"✅ Order {order_no} cancelled.")
            import time; time.sleep(0.3)
            st.rerun()
            return True
        except Exception as _e:
            st.error(f"Cancel failed: {_e}")

    return False
