"""
founder/control_dashboard.py
==============================
Founder Control Tower — real-time ERP health dashboard.

WHAT IT SHOWS
-------------
  1. Error pulse   — error count in last 24h (from audit_log)
  2. Auto-route %  — what % of order items were auto-routed
  3. Supplier load — top 5 suppliers by PO volume today
  4. Kill switches — live view of all SYSTEM_FLAGS

HOW TO USE
----------
  In app.py page routing:

    if st.session_state.get("bo_view_mode") == "founder_dashboard":
        from modules.founder.control_dashboard import render_control_dashboard
        render_control_dashboard()

  Or standalone page:

    from modules.founder.control_dashboard import render_control_dashboard
    render_control_dashboard()

ARCHITECTURE
------------
  All DB calls isolated in get_* functions.
  render_control_dashboard() is the only Streamlit function.
  Safe no-op if DB unavailable.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# DATA FUNCTIONS (pure — no st.*)
# ═══════════════════════════════════════════════════════════════════════

def get_error_pulse() -> int:
    """
    Count errors in the last 24 hours from audit_log.
    Returns 0 on any DB failure.
    """
    since = datetime.now() - timedelta(hours=24)
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT COUNT(*) AS cnt FROM audit_log "
            "WHERE event = 'error' AND created_at > %(since)s",
            {"since": since}
        )
        return int((rows or [{}])[0].get("cnt", 0))
    except Exception as e:
        log.debug(f"[ControlDashboard] get_error_pulse failed: {e}")
        return 0


def get_auto_route_metric() -> float:
    """
    % of order items that were auto-routed.
    Returns 0.0 if no data.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                SUM(CASE WHEN auto_routed = TRUE THEN 1 ELSE 0 END) AS auto_count,
                COUNT(*)                                              AS total
            FROM order_items
        """) or [{}]
        r = rows[0]
        total = int(r.get("total") or 0)
        auto  = int(r.get("auto_count") or 0)
        return round((auto / total) * 100, 2) if total else 0.0
    except Exception as e:
        log.debug(f"[ControlDashboard] get_auto_route_metric failed: {e}")
        return 0.0


def get_supplier_load() -> List[Dict]:
    """
    Top 5 suppliers by PO count created today.
    Returns [] on DB failure.
    """
    today = datetime.now().date()
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT p.party_name AS supplier, COUNT(so.id) AS po_count
            FROM supplier_orders so
            JOIN parties p ON p.id = so.supplier_id
            WHERE DATE(so.created_at) = %(today)s
            GROUP BY p.party_name
            ORDER BY po_count DESC
            LIMIT 5
        """, {"today": today}) or []
    except Exception as e:
        log.debug(f"[ControlDashboard] get_supplier_load failed: {e}")
        return []


def get_kill_switches() -> Dict[str, bool]:
    """
    Current state of all SYSTEM_FLAGS.
    Returns defaults if flags module unavailable.
    """
    try:
        from modules.flags.feature_flags import SYSTEM_FLAGS
        return dict(SYSTEM_FLAGS)
    except Exception as e:
        log.debug(f"[ControlDashboard] get_kill_switches failed: {e}")
        return {}


def get_order_pulse() -> Dict:
    """
    Orders created + saved in the last 24h for headline metrics.
    """
    since = datetime.now() - timedelta(hours=24)
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'PENDING')   AS pending,
                COUNT(*) FILTER (WHERE status = 'CONFIRMED') AS confirmed,
                COUNT(*) FILTER (WHERE status = 'BILLED')    AS billed,
                COUNT(*)                                      AS total
            FROM orders
            WHERE created_at >= %(since)s
        """, {"since": since}) or [{}]
        return rows[0] or {}
    except Exception as e:
        log.debug(f"[ControlDashboard] get_order_pulse failed: {e}")
        return {}


def get_advisory_pulse() -> Dict:
    """Advisory PO summary for last 24h."""
    since = datetime.now() - timedelta(hours=24)
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'Draft')    AS draft_pos,
                COUNT(*) FILTER (WHERE status = 'Sent')     AS sent_pos,
                COUNT(*)                                     AS total_pos
            FROM supplier_orders
            WHERE source = 'ADVISORY' AND created_at >= %(since)s
        """, {"since": since}) or [{}]
        return rows[0] or {}
    except Exception as e:
        log.debug(f"[ControlDashboard] get_advisory_pulse failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════
# STREAMLIT RENDER
# ═══════════════════════════════════════════════════════════════════════

def render_control_dashboard() -> None:
    """
    Founder Control Tower — Streamlit dashboard.
    Call from app.py page routing.
    """
    import streamlit as st

    st.title("🏰 Founder Control Tower")
    st.caption(f"Live ERP health — {datetime.now().strftime('%d %b %Y, %H:%M')}")
    st.markdown("---")

    # ── Row 1: Headline metrics ───────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)

    error_count  = get_error_pulse()
    auto_pct     = get_auto_route_metric()
    order_pulse  = get_order_pulse()

    c1.metric(
        "🔴 Errors (24h)",
        error_count,
        delta=None,
        help="Count of error events in audit_log",
    )
    c2.metric(
        "⚡ Auto-Route %",
        f"{auto_pct}%",
        help="% of order items auto-routed by the system",
    )
    c3.metric(
        "📋 Orders (24h)",
        int(order_pulse.get("total") or 0),
        help="Orders created in last 24 hours",
    )
    c4.metric(
        "💰 Billed (24h)",
        int(order_pulse.get("billed") or 0),
        help="Orders reaching BILLED status in last 24h",
    )

    st.markdown("---")

    # ── Row 2: Advisory + Supplier load ──────────────────────────────
    col_adv, col_sup = st.columns(2)

    with col_adv:
        st.markdown("### 🛒 Advisory POs (24h)")
        adv = get_advisory_pulse()
        if adv:
            a1, a2, a3 = st.columns(3)
            a1.metric("Total",    int(adv.get("total_pos") or 0))
            a2.metric("Sent",     int(adv.get("sent_pos") or 0))
            a3.metric("Draft",    int(adv.get("draft_pos") or 0))
        else:
            st.caption("No advisory PO data")

    with col_sup:
        st.markdown("### 🚚 Top Suppliers Today")
        supplier_load = get_supplier_load()
        if supplier_load:
            for row in supplier_load:
                st.caption(
                    f"**{row.get('supplier', '?')}** — {row.get('po_count', 0)} POs"
                )
        else:
            st.caption("No supplier orders today")

    st.markdown("---")

    # ── Row 3: Kill switches ──────────────────────────────────────────
    st.markdown("### 🎛️ System Flags (Kill Switches)")
    flags = get_kill_switches()

    if not flags:
        st.caption("Flags not available")
    else:
        # Split into two columns for readability
        flag_items = list(flags.items())
        mid = len(flag_items) // 2 + len(flag_items) % 2
        fcol1, fcol2 = st.columns(2)

        for i, (key, val) in enumerate(flag_items):
            col = fcol1 if i < mid else fcol2
            with col:
                if val:
                    st.success(f"✅ `{key}`")
                else:
                    st.warning(f"⛔ `{key}`")

    st.markdown("---")
    st.caption("🔒 Control Tower — read-only view. Flag changes via admin panel or app.py config.")
