"""
modules/system_health.py
==========================
System Health Dashboard — WIN16 DV ERP
Checks DB, loader core, Excel engine, pricing engine, and flag system.
"""

import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


def get_module_health() -> Dict[str, dict]:
    """
    Returns health status for all subsystems.
    Each entry: {ok: bool, label: str, detail: str, ms: float}
    """
    checks = {}

    def _check(name, label, fn):
        t0 = time.time()
        try:
            detail = fn()
            ms = round((time.time() - t0) * 1000, 1)
            checks[name] = {"ok": True, "label": label, "detail": detail or "OK", "ms": ms}
        except Exception as e:
            ms = round((time.time() - t0) * 1000, 1)
            checks[name] = {"ok": False, "label": label, "detail": str(e)[:80], "ms": ms}

    # Database ping
    def _db():
        from modules.sql_adapter import run_query
        r = run_query("SELECT version() AS v")
        v = r[0]["v"] if r else "connected"
        return v[:40] if v else "connected"
    _check("database", "Database", _db)

    # Excel / pandas engine
    def _excel():
        import pandas as pd
        import openpyxl
        return f"pandas {pd.__version__} | openpyxl {openpyxl.__version__}"
    _check("excel_engine", "Excel Engine", _excel)

    # Loader core
    def _loader():
        from modules.loaders.universal_loader_core import run_loader, LOADER_MAP
        return f"{len(LOADER_MAP)} file types registered"
    _check("loader_core", "Loader Core", _loader)

    # Schema guard
    def _schema():
        from modules.loaders.schema_guard import KNOWN_SCHEMA
        return f"{len(KNOWN_SCHEMA)} schemas loaded"
    _check("schema_guard", "Schema Guard", _schema)

    # Feature flags
    def _flags():
        from modules.loaders.feature_flags import get_flag, _DEFAULTS
        count = len(_DEFAULTS)
        return f"{count} flags configured"
    _check("feature_flags", "Feature Flags", _flags)

    # Pricing engine
    def _pricing():
        from modules.pricing.pricing_engine import money
        return "OK"
    _check("pricing_engine", "Pricing Engine", _pricing)

    return checks


def get_health_summary(checks: Dict[str, dict]) -> dict:
    total = len(checks)
    ok_count = sum(1 for c in checks.values() if c["ok"])
    pct = round(ok_count / total * 100) if total else 0
    overall = "healthy" if pct == 100 else ("degraded" if pct >= 60 else "critical")
    return {"total": total, "ok": ok_count, "pct": pct, "status": overall}


# ─────────────────────────────────────────────────────────────────────────────
# OBSERVABILITY PANEL (extends get_module_health)
# Shows error log, slow queries, and audit coverage in Admin tab
# ─────────────────────────────────────────────────────────────────────────────

def render_observability_panel():
    """
    Admin panel — central error log viewer + slow query tracker.
    Add to Admin tab in loader_ui.py:
        from modules.ui.system_health import render_observability_panel
        render_observability_panel()
    """
    import streamlit as st

    st.markdown("#### 📡 Observability — Errors & Performance")

    tab_errors, tab_slow, tab_coverage = st.tabs(
        ["🔴 Error Log", "🐢 Slow Queries", "✅ Audit Coverage"]
    )

    # ── Error Log ─────────────────────────────────────────────────────────────
    with tab_errors:
        try:
            from modules.core.error_logger import get_recent_errors
            c1, c2 = st.columns([3, 1])
            with c1:
                ctx_filter = st.text_input("Filter by context", key="_obs_ctx")
            with c2:
                limit = st.selectbox("Show last", [25, 50, 100, 200], key="_obs_limit")

            errors = get_recent_errors(limit=limit, context_filter=ctx_filter or None)

            if not errors:
                st.success("✅ No errors recorded")
            else:
                st.warning(f"⚠️ {len(errors)} error(s) in log")
                for e in errors:
                    etype = e.get("error_type", "Error")
                    msg   = e.get("error_msg", "")[:120]
                    ctx   = e.get("context", "")
                    ts    = str(e.get("created_at", ""))[:19]
                    icon  = "🐌" if etype == "SLOW_QUERY" else "🔴"
                    with st.expander(f"{icon} `{ts}` [{ctx}] {etype}: {msg}"):
                        payload = e.get("payload") or {}
                        if payload:
                            st.json(payload)
                        tb = e.get("traceback", "")
                        if tb and tb.strip() and etype != "SLOW_QUERY":
                            st.code(tb[-1500:], language="python")
        except Exception as ex:
            st.info(f"Error log not available: {ex} — run SQL migration first.")

    # ── Slow Queries ──────────────────────────────────────────────────────────
    with tab_slow:
        try:
            from modules.core.error_logger import get_recent_errors
            slow = get_recent_errors(limit=50, context_filter="slow")
            if not slow:
                st.success("✅ No slow queries recorded")
            else:
                for s in slow:
                    ts      = str(s.get("created_at", ""))[:19]
                    ctx     = s.get("context", "")
                    msg     = s.get("error_msg", "")
                    payload = s.get("payload") or {}
                    elapsed = payload.get("elapsed_ms", "?")
                    st.markdown(f"🐌 `{ts}` **{ctx}** — {elapsed}ms")
                    if payload.get("sql_prefix"):
                        st.caption(f"SQL: {payload['sql_prefix']}")
        except Exception:
            st.info("Slow query log not available — run SQL migration first.")

    # ── Audit Coverage ────────────────────────────────────────────────────────
    with tab_coverage:
        st.caption("Actions logged to audit_logs in the last 7 days")
        try:
            from modules.sql_adapter import run_query
            rows = run_query("""
                SELECT action, COUNT(*) as count,
                       MAX(created_at)::text as last_seen
                FROM audit_logs
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY action
                ORDER BY count DESC
            """) or []

            if not rows:
                st.info("No audit events in last 7 days")
            else:
                total = sum(r.get("count", 0) for r in rows)
                st.metric("Total audit events (7 days)", total)
                for r in rows:
                    count   = r.get("count", 0)
                    action  = r.get("action", "")
                    last    = str(r.get("last_seen", ""))[:19]
                    pct     = int(count / total * 100) if total else 0
                    st.markdown(
                        f"`{action}` — **{count}** events ({pct}%) | last: {last}"
                    )
        except Exception:
            st.info("Audit coverage not available — run SQL migration first.")
