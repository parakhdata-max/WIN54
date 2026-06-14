"""
ERP System Health Monitor
=========================
Runs data integrity checks across all billing, production, and supplier tables.
Drop into:  modules/admin/system_health.py
Add to nav: if page == "System Health": render_system_health()

All checks should return 0 issues on a healthy system.
"""

import streamlit as st
from datetime import date
from typing import List, Tuple


def _q(sql: str, params: dict = None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


def _count(sql: str, params: dict = None) -> int:
    rows = _q(sql, params)
    return len(rows)


# ── Individual checks ──────────────────────────────────────────────────────────

def _check_overbilling() -> Tuple[int, list]:
    rows = _q("""
        SELECT o.order_no, ol.id AS line_id,
               ol.quantity, ol.billed_qty,
               (ol.billed_qty - ol.quantity) AS over_by
        FROM   order_lines ol
        JOIN   orders o ON o.id = ol.order_id
        WHERE  COALESCE(ol.billed_qty, 0) > ol.quantity
    """)
    return len(rows), rows


def _check_billed_status_mismatch() -> Tuple[int, list]:
    rows = _q("""
        SELECT o.order_no,
               SUM(ol.quantity)               AS total_qty,
               SUM(COALESCE(ol.billed_qty,0)) AS billed_qty
        FROM   orders o
        JOIN   order_lines ol ON o.id = ol.order_id
        WHERE  o.status = 'BILLED'
          AND  o.is_deleted = FALSE
        GROUP  BY o.order_no
        HAVING SUM(COALESCE(ol.billed_qty,0)) < SUM(ol.quantity)
    """)
    return len(rows), rows


def _check_challan_exceeds_order() -> Tuple[int, list]:
    rows = _q("""
        SELECT o.order_no, ol.id AS line_id,
               ol.quantity AS ordered_qty,
               SUM(cl.quantity) AS challan_qty
        FROM   order_lines ol
        JOIN   orders o ON o.id = ol.order_id
        JOIN   challan_lines cl ON cl.order_line_id = ol.id
        GROUP  BY o.order_no, ol.id, ol.quantity
        HAVING SUM(cl.quantity) > ol.quantity
    """)
    return len(rows), rows


def _check_invoice_exceeds_challan() -> Tuple[int, list]:
    rows = _q("""
        SELECT cl.order_line_id,
               SUM(cl.quantity)             AS challan_qty,
               SUM(COALESCE(il.quantity,0)) AS invoice_qty
        FROM   challan_lines cl
        LEFT   JOIN invoice_lines il ON il.order_line_id = cl.order_line_id
        GROUP  BY cl.order_line_id
        HAVING SUM(COALESCE(il.quantity,0)) > SUM(cl.quantity)
    """)
    return len(rows), rows


def _check_empty_orders() -> Tuple[int, list]:
    rows = _q("""
        SELECT o.order_no
        FROM   orders o
        LEFT   JOIN order_lines ol ON ol.order_id = o.id
        WHERE  ol.id IS NULL
          AND  o.is_deleted = FALSE
    """)
    return len(rows), rows


def _check_missing_jobs() -> Tuple[int, list]:
    rows = _q("""
        SELECT o.order_no, ol.id AS line_id
        FROM   order_lines ol
        JOIN   orders o ON o.id = ol.order_id
        LEFT   JOIN job_master jm ON jm.order_line_id = ol.id
        WHERE  COALESCE(ol.lens_params->>'manufacturing_route','STOCK') = 'INHOUSE'
          AND  jm.id IS NULL
          AND  o.is_deleted = FALSE
    """)
    return len(rows), rows


def _check_supplier_pending() -> Tuple[int, list]:
    rows = _q("""
        SELECT so.id AS supplier_order_id,
               SUM(soi.ordered_qty)   AS ordered,
               SUM(soi.received_qty)  AS received
        FROM   supplier_order_items soi
        JOIN   supplier_orders so ON so.id = soi.supplier_order_id
        GROUP  BY so.id
        HAVING SUM(COALESCE(soi.received_qty,0)) < SUM(soi.ordered_qty)
    """)
    return len(rows), rows


def _check_negative_stock() -> Tuple[int, list]:
    rows = _q("""
        SELECT product_id, quantity
        FROM   inventory_stock
        WHERE  quantity < 0
    """)
    return len(rows), rows


def _check_duplicate_challan_lines() -> Tuple[int, list]:
    rows = _q("""
        SELECT challan_id, order_line_id, COUNT(*) AS cnt
        FROM   challan_lines
        GROUP  BY challan_id, order_line_id
        HAVING COUNT(*) > 1
    """)
    return len(rows), rows


def _check_duplicate_invoice_lines() -> Tuple[int, list]:
    rows = _q("""
        SELECT invoice_id, order_line_id, COUNT(*) AS cnt
        FROM   invoice_lines
        GROUP  BY invoice_id, order_line_id
        HAVING COUNT(*) > 1
    """)
    return len(rows), rows


def _check_document_number_integrity() -> Tuple[int, list]:
    try:
        from modules.db.order_number_registry import audit_document_number_integrity
        rows = audit_document_number_integrity() or []
        return len(rows), rows
    except Exception as exc:
        return 1, [{"error": str(exc)}]


# ── Render ─────────────────────────────────────────────────────────────────────

def render_system_health():
    st.title("🩺 ERP System Health Monitor")
    st.caption(f"Last checked: {date.today().strftime('%d %b %Y')} — All checks should show ✅ OK")

    # ── PostgreSQL / deployment health ───────────────────────────────────────
    try:
        from modules.sql_adapter import get_database_info
        dbi = get_database_info() or {}
        st.markdown("### PostgreSQL Health")
        d1, d2, d3, d4, d5 = st.columns(5)
        d1.metric("Mode", str(dbi.get("deployment_mode") or "—"))
        d2.metric("Database", str(dbi.get("database") or "—"))
        d3.metric("Connections", f"{int(dbi.get('active_connections') or 0)} / {int(dbi.get('max_connections') or 0)}")
        d4.metric("Waiting Locks", int(dbi.get("waiting_locks") or 0))
        _size_mb = int(dbi.get("db_size_bytes") or 0) / (1024 * 1024)
        d5.metric("DB Size", f"{_size_mb:,.1f} MB")
        st.caption(
            f"Host: {dbi.get('host') or '—'} · Server: {dbi.get('server_addr') or '—'}:{dbi.get('server_port') or '—'} · "
            f"Role: {'Standby/replica' if dbi.get('standby') else 'Primary'}"
        )
        if str(dbi.get("status")) != "ok":
            st.error(f"PostgreSQL connection issue: {dbi.get('error') or 'unknown'}")
        elif int(dbi.get("waiting_locks") or 0) > 0:
            st.warning("PostgreSQL has waiting locks. If saves feel stuck, check long transactions before repeated posting.")
        elif int(dbi.get("max_connections") or 0) and int(dbi.get("active_connections") or 0) > int(dbi.get("max_connections") or 1) * 0.8:
            st.warning("PostgreSQL connection usage is high. LAN/cloud workers may need pooling or old sessions closed.")
        else:
            st.success("PostgreSQL connection health looks OK.")
    except Exception as _dbi_e:
        st.warning(f"PostgreSQL health unavailable: {_dbi_e}")

    checks = [
        ("Overbilling Errors",              _check_overbilling),
        ("BILLED Orders with Incomplete Lines", _check_billed_status_mismatch),
        ("Challan Qty Exceeds Order Qty",    _check_challan_exceeds_order),
        ("Invoice Qty Exceeds Challan Qty",  _check_invoice_exceeds_challan),
        ("Orders Without Lines",             _check_empty_orders),
        ("INHOUSE Lines Missing Job Cards",  _check_missing_jobs),
        ("Supplier Orders Pending Receipt",  _check_supplier_pending),
        ("Negative Inventory Stock",         _check_negative_stock),
        ("Duplicate Challan Lines",          _check_duplicate_challan_lines),
        ("Duplicate Invoice Lines",          _check_duplicate_invoice_lines),
        ("Document Number Integrity",        _check_document_number_integrity),
    ]

    results = []
    total_issues = 0

    with st.spinner("Running health checks…"):
        for name, fn in checks:
            count, rows = fn()
            total_issues += count
            results.append((name, count, rows))

    # ── Summary KPIs ──────────────────────────────────────────────────────────
    ok_count  = sum(1 for _, c, _ in results if c == 0)
    err_count = len(results) - ok_count

    k1, k2, k3 = st.columns(3)
    k1.metric("Checks Run",   len(results))
    k2.metric("✅ Passing",   ok_count)
    k3.metric("❌ Issues",    err_count,
              delta=None if err_count == 0 else f"{total_issues} total rows")

    st.markdown("---")

    # ── Per-check results ─────────────────────────────────────────────────────
    for name, count, rows in results:
        if count == 0:
            st.success(f"✅  {name}  —  OK")
        else:
            st.error(f"❌  {name}  —  {count} issue(s) found")
            with st.expander(f"Show details ({count} rows)"):
                st.dataframe(rows, width='stretch')

    st.markdown("---")

    # ── Overall verdict ───────────────────────────────────────────────────────
    if total_issues == 0:
        st.success("🎉  **System Health: PERFECT** — No data integrity issues detected.")
    else:
        st.warning(
            f"⚠️  **{total_issues} issue(s) found across {err_count} check(s).**  "
            "Expand the red checks above for details."
        )

    # ── Audit Log Health ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📋 Audit Log Health")

    try:
        from modules.backoffice.audit_logger import get_audit_stats, archive_old_entries
        stats = get_audit_stats()
        if stats:
            m1,m2,m3,m4,m5 = st.columns(5)
            m1.metric("Total Entries",  f"{int(stats.get('total_rows') or 0):,}")
            m2.metric("Last 7 Days",    f"{int(stats.get('last_7_days') or 0):,}")
            m3.metric("Last 30 Days",   f"{int(stats.get('last_30_days') or 0):,}")
            m4.metric("Table Size",     str(stats.get('table_size','—')))
            m5.metric("Oldest Entry",   str(stats.get('oldest_entry','—')))

            # Growth rate warning
            daily_rate = int(stats.get('last_7_days') or 0) / 7
            if daily_rate > 500:
                st.warning(
                    f"⚠️ High audit log growth: ~{daily_rate:.0f} entries/day. "
                    "Consider archiving entries older than 90 days."
                )

            # Archive button (only non-financial entries)
            with st.expander("🗑️ Archive old entries (keeps financial audit permanently)"):
                days = st.number_input("Keep entries newer than (days)", 
                                       value=90, min_value=30, step=30, 
                                       key="audit_archive_days")
                st.caption(
                    "⚠️ This deletes operational logs older than the threshold. Financial entries are NEVER deleted (invoice, payment, reversal, journal)."
                )
                if st.button("🗑️ Archive Now", key="audit_archive_run",
                              type="secondary"):
                    deleted = archive_old_entries(int(days))
                    if deleted > 0:
                        st.success(f"✅ {deleted:,} old operational entries archived.")
                    else:
                        st.info("Nothing to archive — all entries are within retention period.")
        else:
            st.info("Audit log empty or not yet created.")
    except Exception as _ae:
        st.caption(f"Audit log stats unavailable: {_ae}")

    # ── Financial Reconciliation ─────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 💰 Financial Integrity Checks")

    col_r1, col_r2 = st.columns([4, 1])
    with col_r2:
        force_refresh = st.button("🔄 Refresh", key="recon_refresh")

    try:
        from modules.services.reconciliation_service import run_reconciliation
        recon = run_reconciliation(force=force_refresh)

        # Overall status banner
        overall = recon.get("overall", "ok")
        errs    = recon.get("errors",   0)
        warns   = recon.get("warnings", 0)
        cache_note = f" · cached {recon.get('cache_age_s',0)}s ago" if recon.get("from_cache") else ""

        if overall == "ok":
            st.success(f"🟢  **System Healthy** — all financial checks passed{cache_note}  ·  {recon.get('run_at','')}")
        elif overall == "warning":
            st.warning(f"🟡  **{warns} warning(s)** — review needed{cache_note}  ·  {recon.get('run_at','')}")
        else:
            st.error(f"🔴  **{errs} error(s) found** — action required{cache_note}  ·  {recon.get('run_at','')}")

        # Individual check cards
        checks_data = recon.get("checks", [])
        cols = st.columns(len(checks_data)) if checks_data else []

        for i, chk in enumerate(checks_data):
            status = chk.get("status", "ok")
            icon   = "🟢" if status == "ok" else ("🔴" if status == "error" else "🟡")
            label  = chk.get("label", "")
            detail = chk.get("detail", "")
            col    = cols[i] if i < len(cols) else st

            with col:
                st.markdown(
                    f"<div style='background:#0a1628;border:1px solid "
                    f"{'#22c55e' if status=='ok' else ('#ef4444' if status=='error' else '#f59e0b')};"
                    f"border-radius:8px;padding:10px 12px;text-align:center'>"
                    f"<div style='font-size:1.3rem'>{icon}</div>"
                    f"<div style='color:#e2e8f0;font-size:.72rem;font-weight:700;margin-top:2px'>{label}</div>"
                    f"<div style='color:#94a3b8;font-size:.65rem;margin-top:3px'>{detail}</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

        # Detail tables for non-ok checks
        for chk in checks_data:
            if chk.get("status") == "ok":
                continue
            label = chk.get("label","")

            if label == "Invoice vs Ledger" and chk.get("rows"):
                with st.expander(f"⚠️ Invoice vs Ledger mismatches ({chk['count']})", expanded=True):
                    import pandas as pd
                    df = pd.DataFrame(chk["rows"])
                    st.dataframe(df, width='stretch', hide_index=True,
                        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                                       for c in ["invoice_total","ledger_dr","diff"] if c in df.columns})
                    st.caption("💡 Run Accounts → Backfill to post missing journal entries.")

            elif label == "Negative Stock" and chk.get("rows"):
                with st.expander(f"🔴 Negative stock ({chk['count']} blanks)", expanded=True):
                    import pandas as pd
                    st.dataframe(pd.DataFrame(chk["rows"]), width='stretch', hide_index=True)
                    st.caption("💡 Check job card saves — a blank was over-allocated.")

            elif label == "Orphan Ledger Entries":
                total_orphans = chk.get("count", 0)
                if total_orphans > 0:
                    with st.expander(f"⚠️ Orphan ledger entries ({total_orphans})", expanded=True):
                        import pandas as pd
                        if chk.get("orphan_invoices"):
                            st.caption("Ledger entries with no matching invoice:")
                            st.dataframe(pd.DataFrame(chk["orphan_invoices"]),
                                         width='stretch', hide_index=True)
                        if chk.get("orphan_payments"):
                            st.caption("Ledger entries with no matching payment:")
                            st.dataframe(pd.DataFrame(chk["orphan_payments"]),
                                         width='stretch', hide_index=True)
                        st.caption("💡 These entries reference deleted documents. Raise a reversal journal entry.")

            elif label == "Unposted Documents" and chk.get("count", 0) > 0:
                with st.expander(f"⚠️ Unposted documents ({chk['count']})", expanded=True):
                    ni = chk.get("unposted_invoices", 0)
                    np_ = chk.get("unposted_payments", 0)
                    st.markdown(f"**{ni}** invoices and **{np_}** payments have no journal entry.")
                    st.caption("💡 Go to Accounts → Backfill → Run Backfill Now.")

    except Exception as _re:
        st.warning(f"Reconciliation checks unavailable: {_re}")

    # ── Quick SQL for manual fix ───────────────────────────────────────────────
    with st.expander("🔧 Manual debug queries (copy to pgAdmin)"):
        st.code("""
-- Over-billed lines
SELECT o.order_no, ol.id, ol.quantity, ol.billed_qty
FROM   order_lines ol JOIN orders o ON o.id = ol.order_id
WHERE  COALESCE(ol.billed_qty,0) > ol.quantity;

-- Orders BILLED but incomplete
SELECT o.order_no, SUM(ol.quantity) total, SUM(COALESCE(ol.billed_qty,0)) billed
FROM   orders o JOIN order_lines ol ON o.id = ol.order_id
WHERE  o.status='BILLED' GROUP BY o.order_no
HAVING SUM(COALESCE(ol.billed_qty,0)) < SUM(ol.quantity);

-- Master billing summary
SELECT
    COUNT(*) FILTER (WHERE COALESCE(billed_qty,0) > quantity) AS overbilling_errors,
    COUNT(*) FILTER (WHERE COALESCE(billed_qty,0) < quantity) AS pending_billing
FROM order_lines;
        """, language="sql")


if __name__ == "__main__":
    render_system_health()
