"""
backoffice/ux_booster.py
=========================
Operator Experience Boosters — Priority 6.

FEATURES
--------
  1. Keyboard shortcuts (Ctrl+S, Ctrl+A injected via JS)
  2. Inline warnings (supplier delay risk, low margin, unallocated)
  3. Sticky context (remembers last supplier, last lab used)

ARCHITECTURE
------------
  Call render_ux_boosters(ctx) once at the TOP of render_order_detail()
  inside backoffice_shell.py, BEFORE the tabs.

  add in backoffice_shell.py:
      from .ux_booster import render_ux_boosters, render_inline_warnings
      render_ux_boosters(ctx)          # keyboard shortcuts + sticky hints
      render_inline_warnings(ctx)      # contextual order-level warnings

PUBLIC API
----------
  render_ux_boosters(ctx)
      Injects keyboard shortcuts + shows sticky-context banner.

  render_inline_warnings(ctx)
      Shows 0-3 contextual warning banners based on order state.

  get_sticky_context(session) → dict
      Returns {last_supplier, last_lab} from session.

  set_sticky_context(session, supplier=None, lab=None)
      Called after successful supplier/lab assignments.
"""

import streamlit as st
from typing import Dict, Optional

_STICKY_KEY = "_bo_sticky_context"

# ── Thresholds for inline warnings ───────────────────────────────────
LOW_MARGIN_THRESHOLD   = 0.15   # gross margin below 15% = warning
DELAY_RISK_DAYS        = 5      # supplier avg delivery > 5d = warning


# ═══════════════════════════════════════════════════════════════════════
# MAIN BOOSTERS ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def render_ux_boosters(ctx) -> None:
    """
    Call once per order detail render — before tabs.

    Injects:
      - Keyboard shortcut JS (Ctrl+S → save, Ctrl+A → assign)
      - Sticky context hint (shows last used supplier/lab)
    """
    _inject_keyboard_shortcuts()
    _render_sticky_context_hint(ctx)


# ═══════════════════════════════════════════════════════════════════════
# 1. KEYBOARD SHORTCUTS
# ═══════════════════════════════════════════════════════════════════════

def _inject_keyboard_shortcuts() -> None:
    """
    Inject JS to intercept Ctrl+S and Ctrl+A.

    Ctrl+S → clicks the #save-btn button (set by final_save_order)
    Ctrl+A → clicks the #assign-btn button (set by assign_all_btn)

    Works by adding data-testid attributes via Streamlit's button key
    which maps to the aria-label in the DOM.
    """
    st.markdown("""
    <script>
    (function() {
        document.addEventListener('keydown', function(e) {
            // Ctrl+S or Cmd+S — save
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                const saveBtn = document.querySelector(
                    'button[data-testid="baseButton-primary"][kind="primaryFormSubmit"],' +
                    'button[aria-label="SAVE TO ORDER"]'
                );
                if (saveBtn) { saveBtn.click(); }
            }
            // Ctrl+A or Cmd+A — confirm assignments
            if ((e.ctrlKey || e.metaKey) && e.key === 'a' && !e.shiftKey) {
                e.preventDefault();
                const assignBtn = document.querySelector(
                    'button[aria-label="Confirm All Assignments"],' +
                    'button[data-testid*="assign_all"]'
                );
                if (assignBtn) { assignBtn.click(); }
            }
        }, true);
    })();
    </script>
    """, unsafe_allow_html=True)

    # Visual hint shown once per session
    hint_key = "_bo_shortcuts_shown"
    if not st.session_state.get(hint_key):
        st.caption("⌨️ **Shortcuts:** Ctrl+S = Save  ·  Ctrl+A = Confirm Assignments")
        st.session_state[hint_key] = True


# ═══════════════════════════════════════════════════════════════════════
# 2. INLINE WARNINGS
# ═══════════════════════════════════════════════════════════════════════

def render_inline_warnings(ctx) -> None:
    """
    Show 0–3 contextual warning banners based on the current order state.
    Place this just above the tab section in backoffice_shell.py.

    Checks:
      - Supplier delay risk (if assigned supplier has slow delivery history)
      - Low margin (if any line has margin below threshold)
      - Unallocated stock lines
    """
    warnings = []

    # Check 1: Supplier delay risk
    delay_warning = _check_supplier_delay_risk(ctx)
    if delay_warning:
        warnings.append(("warning", delay_warning))

    # Check 2: Low margin lines
    margin_warning = _check_low_margin(ctx)
    if margin_warning:
        warnings.append(("warning", margin_warning))

    # Check 3: Unallocated lines
    alloc_warning = _check_unallocated_lines(ctx)
    if alloc_warning:
        warnings.append(("info", alloc_warning))

    for level, msg in warnings:
        getattr(st, level)(msg)


def _check_supplier_delay_risk(ctx) -> Optional[str]:
    """Return warning string if assigned supplier has high avg delivery days."""
    try:
        from modules.procurement.supplier_intelligence import get_scored_suppliers
        assignments = ctx.session.get("bo_assignments", {})
        if not assignments:
            return None

        supplier_ids = {
            str(a.get("supplier_id"))
            for a in assignments.values()
            if a.get("supplier_id")
        }
        if not supplier_ids:
            return None

        scored = get_scored_suppliers()
        slow = [
            s["name"] for s in scored
            if str(s.get("id")) in supplier_ids
            and float(s.get("delivery_days_avg") or 0) > DELAY_RISK_DAYS
        ]
        if slow:
            return f"⏰ Supplier delay risk: {', '.join(slow)} — avg delivery > {DELAY_RISK_DAYS}d"
    except Exception:
        pass
    return None


def _check_low_margin(ctx) -> Optional[str]:
    """Return warning string if any line has margin below threshold."""
    try:
        low_lines = []
        for line in ctx.all_lines:
            billing_total = float(line.get("billing_total") or 0)
            cost          = float(line.get("cost") or line.get("avg_cost") or 0)
            if billing_total > 0 and cost > 0:
                margin = (billing_total - cost) / billing_total
                if margin < LOW_MARGIN_THRESHOLD:
                    low_lines.append(line.get("product_name", "N/A"))
        if low_lines:
            return (
                f"💸 Low margin warning on: {', '.join(low_lines[:3])}"
                + (" +" + str(len(low_lines) - 3) + " more" if len(low_lines) > 3 else "")
            )
    except Exception:
        pass
    return None


def _check_unallocated_lines(ctx) -> Optional[str]:
    """Return info string if any lines have pending (unallocated) qty."""
    unalloc = []
    for line in ctx.all_lines:
        billing_qty  = int(line.get("billing_qty") or 0)
        allocated    = int(line.get("allocated_qty") or 0)
        pending      = max(0, billing_qty - allocated)
        if pending > 0:
            unalloc.append(line.get("product_name", "?"))
    if unalloc:
        return (
            f"📦 {len(unalloc)} unallocated line"
            + ("s" if len(unalloc) > 1 else "")
            + f": {', '.join(unalloc[:3])}"
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
# 3. STICKY CONTEXT
# ═══════════════════════════════════════════════════════════════════════

def get_sticky_context(session) -> Dict:
    """
    Returns {last_supplier_id, last_supplier_name, last_lab, last_lab_id}.
    All values may be None.
    """
    return session.get(_STICKY_KEY, {
        "last_supplier_id":   None,
        "last_supplier_name": None,
        "last_lab":           None,
    })


def set_sticky_context(
    session,
    supplier_id:   Optional[str] = None,
    supplier_name: Optional[str] = None,
    lab:           Optional[str] = None,
) -> None:
    """
    Called after successful supplier or lab assignment.
    Use in fulfillment/ui.py after supplier order is created.

    Example:
        set_sticky_context(ctx.session,
                           supplier_id="SUP001", supplier_name="Shamir")
    """
    current = session.get(_STICKY_KEY, {})
    if supplier_id:
        current["last_supplier_id"]   = supplier_id
        current["last_supplier_name"] = supplier_name
    if lab:
        current["last_lab"] = lab
    session[_STICKY_KEY] = current


def _render_sticky_context_hint(ctx) -> None:
    """Show a subtle 'Last used:' banner if sticky context is set."""
    sticky = get_sticky_context(ctx.session)
    parts  = []

    if sticky.get("last_supplier_name"):
        parts.append(f"🏭 Last supplier: **{sticky['last_supplier_name']}**")
    if sticky.get("last_lab"):
        parts.append(f"🔬 Last lab: **{sticky['last_lab']}**")

    if parts:
        st.caption("  ·  ".join(parts) + " _(sticky context from last order)_")
