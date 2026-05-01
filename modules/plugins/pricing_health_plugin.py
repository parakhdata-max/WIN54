"""
modules/backoffice/plugins/pricing_health_plugin.py
====================================================
Live Pricing & Finalize Health Dashboard.

Auto-discovered by the plugin registry — no manual wiring needed.
Adds a "🛡️ Price Health" tab to every order's backoffice view.

Shows:
  - Per-order pricing integrity check (unit_price, total_price, gst_amount)
  - Recent finalize audit log (CONFIRMED / REJECTED, pricing warnings)
  - System-wide price health summary (last 500 lines from DB)
  - One-click backfill of missing gst_amount values

PLUGIN_META controls tab label, role restrictions, and sort order.
"""

from __future__ import annotations

import logging
from typing import Optional

import streamlit as st

log = logging.getLogger(__name__)

# ── Plugin metadata — read by registry.discover_plugins() ─────────────────

PLUGIN_META = {
    "id":      "pricing_health",
    "label":   "🛡️ Price Health",
    "tab_key": "tab8",
    "roles":   [],        # [] = visible to all roles
    "enabled": True,
    "order":   40,
}


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT (called by shell)
# ═══════════════════════════════════════════════════════════════════════════

def render(ctx) -> None:
    """Plugin entry point — receives BackofficeContext."""
    order = ctx.order

    st.subheader("🛡️ Pricing & Finalize Health")
    st.caption("Real-time integrity check for this order and system-wide pricing audit")

    tab_order, tab_system, tab_audit = st.tabs([
        "📋 This Order",
        "📊 System Health",
        "📜 Finalize Audit Log",
    ])

    with tab_order:
        _render_order_price_check(order)

    with tab_system:
        _render_system_health()

    with tab_audit:
        _render_finalize_audit_log()


# ═══════════════════════════════════════════════════════════════════════════
# TAB 1 — THIS ORDER
# ═══════════════════════════════════════════════════════════════════════════

def _render_order_price_check(order: dict) -> None:
    """Check pricing integrity for each line of the current order."""

    all_lines = (
        order.get("lines", []) or
        order.get("stock_lines", []) + order.get("inhouse_lines", []) + order.get("lab_order_lines", [])
    )

    if not all_lines:
        st.info("No lines in this order.")
        return

    issues_found = []
    clean_lines  = []

    for idx, line in enumerate(all_lines):
        product   = line.get("product_name", f"Line {idx+1}")
        eye       = line.get("eye_side", "")
        qty       = float(line.get("billing_qty") or 0)
        unit_p    = float(line.get("unit_price") or 0)
        total_p   = float(line.get("billing_total") or line.get("total_price") or 0)
        gst_pct   = float(line.get("gst_percent") or 0)
        gst_amt   = float(line.get("gst_amount") or 0)

        # Skip legitimately empty pending/RX lines
        if qty == 0 and unit_p == 0 and total_p == 0:
            continue

        line_issues = []

        if unit_p == 0 and total_p > 0 and qty > 0:
            line_issues.append(f"unit_price missing (expected ₹{total_p/qty:.2f})")
        if total_p == 0 and unit_p > 0 and qty > 0:
            line_issues.append(f"total_price missing (expected ₹{unit_p*qty:.2f})")
        if unit_p == 0 and total_p == 0 and qty > 0:
            line_issues.append("price completely lost — qty present but no price")
        if gst_pct > 0 and gst_amt == 0 and total_p > 0:
            expected_gst = round(total_p - total_p / (1 + gst_pct / 100), 2)
            line_issues.append(f"gst_amount=0 (expected ₹{expected_gst:.2f} @ {gst_pct:.0f}%)")

        if line_issues:
            issues_found.append({
                "product": product,
                "eye": eye,
                "qty": qty,
                "unit_price": unit_p,
                "total_price": total_p,
                "issues": line_issues,
            })
        else:
            clean_lines.append(line)

    # ── Summary metrics ───────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Lines", len(all_lines))
    c2.metric("✅ Clean",    len(clean_lines))
    c3.metric("⚠️ Issues",   len(issues_found))

    st.markdown("---")

    if not issues_found:
        st.success("✅ All lines in this order have correct pricing data.")
        _render_order_pricing_summary(all_lines)
        return

    st.error(f"❌ {len(issues_found)} line(s) have pricing problems:")

    for row in issues_found:
        with st.container(border=True):
            col_a, col_b, col_c, col_d = st.columns([3, 1, 1, 1])
            _eye_tag = f"({row['eye']})" if row['eye'] else ""
            col_a.markdown(f"**{row['product']}** {_eye_tag}")
            col_b.metric("Qty",   f"{row['qty']:.0f}")
            col_c.metric("Unit",  f"₹{row['unit_price']:.2f}")
            col_d.metric("Total", f"₹{row['total_price']:.2f}")
            for issue in row["issues"]:
                st.warning(f"⚠️ {issue}")

    st.markdown("---")
    _render_order_pricing_summary(all_lines)


def _render_order_pricing_summary(lines: list) -> None:
    """Show pricing totals for reference."""
    total_val = sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in lines)
    total_gst = sum(float(l.get("gst_amount") or 0) for l in lines)
    base_val  = round(total_val - total_gst, 2)

    st.markdown("#### Pricing Summary")
    c1, c2, c3 = st.columns(3)
    c1.metric("Base Value",    f"₹{base_val:,.2f}")
    c2.metric("GST Amount",    f"₹{total_gst:,.2f}")
    c3.metric("Grand Total",   f"₹{total_val:,.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 2 — SYSTEM HEALTH (DB scan)
# ═══════════════════════════════════════════════════════════════════════════

def _render_system_health() -> None:
    """Scan last 500 order_lines from DB for pricing issues."""

    if st.button("🔍 Run System Scan", key="ph_run_scan", type="primary"):
        st.session_state["ph_scan_result"] = None  # force refresh

    result = st.session_state.get("ph_scan_result")

    if result is None:
        with st.spinner("Scanning last 500 order lines..."):
            result = _run_db_scan(limit=500)
            st.session_state["ph_scan_result"] = result

    if result.get("error"):
        st.error(f"Scan failed: {result['error']}")
        return

    total   = result["total"]
    corrupt = result["corrupt"]
    rows    = result["rows"]

    # ── Metrics ──────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    health_pct = round(100 * (total - corrupt) / total, 1) if total else 100
    c1.metric("Lines Scanned", total)
    c2.metric("✅ Healthy", total - corrupt)
    c3.metric("⚠️ Issues", corrupt)
    c4.metric("Health Score", f"{health_pct}%")

    if corrupt == 0:
        st.success("✅ System pricing data is fully healthy.")
        return

    st.warning(f"⚠️ {corrupt} lines have pricing issues.")
    st.markdown("---")

    # ── Auto-fix button ───────────────────────────────────────────────
    if st.button("🔧 Auto-fix: Backfill missing gst_amount", key="ph_autofix"):
        fixed = _run_gst_backfill()
        if fixed >= 0:
            st.success(f"✅ Backfilled gst_amount on {fixed} rows. Refresh scan to verify.")
            st.session_state["ph_scan_result"] = None
        else:
            st.error("Backfill failed — check DB connection")

    st.markdown("---")
    st.markdown("#### Corrupt Rows (first 50)")

    import pandas as pd
    df = pd.DataFrame(rows[:50])

    if not df.empty:
        display_cols = [c for c in [
            "order_no", "order_type", "product_name",
            "billing_qty", "unit_price", "total_price",
            "gst_percent", "gst_amount", "_issues"
        ] if c in df.columns]
        df["_issues"] = df["_issues"].apply(lambda x: "; ".join(x) if isinstance(x, list) else str(x))
        st.dataframe(df[display_cols], use_container_width=True, height=350)


def _run_db_scan(limit: int = 500) -> dict:
    """Query DB and classify price issues. Returns summary dict."""
    try:
        from modules.sql_adapter import run_query

        rows = run_query(f"""
            SELECT
                o.order_no,
                o.order_type,
                ol.id           AS line_id,
                COALESCE(p.product_name, 'Unknown')  AS product_name,
                COALESCE(ol.quantity, 0)             AS billing_qty,
                COALESCE(ol.unit_price, 0)           AS unit_price,
                COALESCE(ol.total_price, 0)          AS total_price,
                COALESCE(p.gst_percent, 0)           AS gst_percent,
                COALESCE(ol.gst_amount, 0)           AS gst_amount
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            ORDER BY o.created_at DESC
            LIMIT {int(limit)}
        """)

        corrupt = []
        for row in rows:
            qty       = float(row.get("billing_qty") or 0)
            unit_p    = float(row.get("unit_price")  or 0)
            total_p   = float(row.get("total_price") or 0)
            gst_pct   = float(row.get("gst_percent") or 0)
            gst_amt   = float(row.get("gst_amount")  or 0)

            if qty == 0 and unit_p == 0 and total_p == 0:
                continue  # pending/RX — healthy

            issues = []
            if unit_p == 0 and total_p > 0 and qty > 0:
                issues.append(f"unit_price=0 (expected {round(total_p/qty,2)})")
            if total_p == 0 and unit_p > 0 and qty > 0:
                issues.append(f"total_price=0")
            if unit_p == 0 and total_p == 0 and qty > 0:
                issues.append("price lost")
            if gst_pct > 0 and gst_amt == 0 and total_p > 0:
                issues.append(f"gst_amount=0 (expected {round(total_p - total_p/(1+gst_pct/100),2)})")

            if issues:
                row["_issues"] = issues
                corrupt.append(row)

        return {"total": len(rows), "corrupt": len(corrupt), "rows": corrupt, "error": None}

    except Exception as e:
        log.error(f"[PricingHealth] DB scan failed: {e}")
        return {"total": 0, "corrupt": 0, "rows": [], "error": str(e)}


def _run_gst_backfill() -> int:
    """
    Backfill gst_amount = 0 rows using total_price + gst_percent.
    Returns number of rows fixed, or -1 on error.
    """
    try:
        from modules.sql_adapter import get_transaction_connection, close_connection
        import psycopg2.extras

        conn   = get_transaction_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Retail: back-calc GST from inclusive MRP
        cursor.execute("""
            UPDATE order_lines ol
            SET gst_amount = ROUND(
                ol.total_price - (ol.total_price / (1 + COALESCE(p.gst_percent, 0) / 100.0)),
                2
            )
            FROM products p, orders o
            WHERE p.id = ol.product_id
              AND o.id = ol.order_id
              AND o.order_type = 'RETAIL'
              AND ol.gst_amount = 0
              AND ol.total_price > 0
              AND COALESCE(p.gst_percent, 0) > 0
        """)
        retail_fixed = cursor.rowcount

        # Wholesale/Purchase: add GST on top
        cursor.execute("""
            UPDATE order_lines ol
            SET gst_amount = ROUND(
                ol.total_price * COALESCE(p.gst_percent, 0) / 100.0,
                2
            )
            FROM products p, orders o
            WHERE p.id = ol.product_id
              AND o.id = ol.order_id
              AND o.order_type != 'RETAIL'
              AND ol.gst_amount = 0
              AND ol.total_price > 0
              AND COALESCE(p.gst_percent, 0) > 0
        """)
        wholesale_fixed = cursor.rowcount

        conn.commit()
        cursor.close()
        close_connection(conn)

        total_fixed = retail_fixed + wholesale_fixed
        log.info(f"[PricingHealth] GST backfill: {retail_fixed} retail + {wholesale_fixed} wholesale = {total_fixed} rows")
        return total_fixed

    except Exception as e:
        log.error(f"[PricingHealth] GST backfill failed: {e}")
        return -1


# ═══════════════════════════════════════════════════════════════════════════
# TAB 3 — FINALIZE AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════

def _render_finalize_audit_log() -> None:
    """Show recent finalize audit records from audit.jsonl."""

    n = st.slider("Records to show", min_value=10, max_value=200, value=50, step=10,
                  key="ph_audit_n")

    records = _load_audit_records(n)

    if not records:
        st.info("No finalize audit records found yet.\n\nAudit log is written to `logs/audit.jsonl` on every order submission.")
        return

    # ── Summary metrics ───────────────────────────────────────────────
    confirmed = sum(1 for r in records if r.get("outcome") == "CONFIRMED")
    rejected  = sum(1 for r in records if r.get("outcome") == "REJECTED")
    warned    = sum(1 for r in records if r.get("pricing_warnings"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Records",   len(records))
    c2.metric("✅ Confirmed", confirmed)
    c3.metric("❌ Rejected",  rejected)
    c4.metric("⚠️ With Warnings", warned)

    st.markdown("---")

    # ── Filter ────────────────────────────────────────────────────────
    filter_mode = st.radio(
        "Show", ["All", "Confirmed only", "Rejected only", "With pricing warnings"],
        horizontal=True, key="ph_audit_filter"
    )

    filtered = records
    if filter_mode == "Confirmed only":
        filtered = [r for r in records if r.get("outcome") == "CONFIRMED"]
    elif filter_mode == "Rejected only":
        filtered = [r for r in records if r.get("outcome") == "REJECTED"]
    elif filter_mode == "With pricing warnings":
        filtered = [r for r in records if r.get("pricing_warnings")]

    st.markdown(f"**{len(filtered)} record(s)**")
    st.markdown("---")

    # ── Record list ───────────────────────────────────────────────────
    for rec in filtered:
        outcome    = rec.get("outcome", "?")
        order_id   = rec.get("order_id", "?")
        order_type = rec.get("order_type", "")
        timestamp  = str(rec.get("timestamp", ""))[:19]
        user       = rec.get("user_name", "?")
        warnings   = rec.get("pricing_warnings", [])
        errors     = rec.get("errors", [])
        line_count = rec.get("line_count", "?")

        icon = "✅" if outcome == "CONFIRMED" else "❌"
        header = f"{icon} **{order_id}** — {order_type} — {timestamp} — {user} ({line_count} lines)"

        with st.expander(header, expanded=(outcome == "REJECTED")):
            col_a, col_b = st.columns(2)
            col_a.write(f"**Outcome:** {outcome}")
            col_b.write(f"**Lines:** {line_count}")

            # Pricing trace summary
            pt = rec.get("pricing_trace")
            if pt:
                st.markdown("**Pricing:**")
                p1, p2, p3 = st.columns(3)
                p1.metric("Subtotal",    f"₹{float(pt.get('subtotal', 0)):,.2f}")
                p2.metric("GST",         f"₹{float(pt.get('tax_amount', 0)):,.2f}")
                p3.metric("Final Value", f"₹{float(pt.get('final_value', 0)):,.2f}")

            if warnings:
                st.markdown("**⚠️ Pricing Warnings:**")
                for w in warnings:
                    st.warning(w)

            if errors:
                st.markdown("**❌ Errors:**")
                for e in errors:
                    st.error(e)

            # Per-line pricing trace
            line_traces = pt.get("lines", []) if pt else []
            if line_traces:
                with st.expander("Per-line detail"):
                    for lt in line_traces:
                        source = lt.get("pricing_source", "?")
                        name   = lt.get("product_name", "?")
                        qty    = lt.get("billing_qty", 0)
                        up     = lt.get("unit_price", 0)
                        tp     = lt.get("total_price", 0)
                        err    = lt.get("error", "")
                        icon_l = "⚠️" if err else "✅"
                        st.caption(
                            f"{icon_l} {name} — qty={qty} — "
                            f"₹{up:.2f}/pc — ₹{tp:.2f} total — [{source}]"
                            + (f" — {err}" if err else "")
                        )


def _load_audit_records(n: int) -> list:
    """Load last n records from audit.jsonl."""
    try:
        from modules.core.audit_log import AuditLog
        log_obj  = AuditLog()
        raw      = log_obj.tail(n)

        # Flatten for display — audit_log.record() shape may vary
        result = []
        for rec in raw:
            flat = {
                "outcome":          rec.get("outcome", "?"),
                "order_id":         rec.get("order_id", "?"),
                "order_type":       rec.get("order_info", {}).get("order_type", ""),
                "timestamp":        rec.get("timestamp", ""),
                "user_name":        rec.get("user_name", "?"),
                "line_count":       rec.get("extra", {}).get("_line_count", "?"),
                "errors":           [str(e) for e in rec.get("issues", []) if _is_error(e)],
                "pricing_warnings": _extract_pricing_warnings(rec),
                "pricing_trace":    rec.get("pricing_trace"),
            }
            result.append(flat)

        return list(reversed(result))   # newest first

    except Exception as e:
        log.warning(f"[PricingHealth] Could not load audit log: {e}")
        return []


def _is_error(issue) -> bool:
    """Check if a ValidationIssue (or its serialized form) is an error."""
    if isinstance(issue, dict):
        return issue.get("severity", "").upper() in ("ERROR", "CRITICAL")
    return False


def _extract_pricing_warnings(rec: dict) -> list:
    """Pull pricing warnings out of the audit record."""
    pt = rec.get("pricing_trace")
    if not pt:
        return []
    if isinstance(pt, dict):
        return pt.get("warnings", [])
    # PricingTrace object
    return getattr(pt, "warnings", [])
