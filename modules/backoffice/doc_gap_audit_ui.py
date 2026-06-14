"""
modules/backoffice/doc_gap_audit_ui.py
Document number gap audit panel.
Shows all detected gaps across orders, challans, invoices, dispatches, payments, CN, DN.
Staff can mark each gap with a reason and resolution.
"""
from __future__ import annotations
import datetime
import streamlit as st
from typing import List, Dict


def _q(sql: str, params: dict = None) -> List[Dict]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _rw(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        st.error(f"DB write: {e}")
        return False


def render_doc_gap_audit() -> None:
    """Document number gap audit panel."""
    st.markdown("### 🔢 Document Number Gap Audit")
    st.caption(
        "Tracks missing sequence numbers across all document types. "
        "Every gap must be explained — abandoned entry, test data, system error, etc."
    )

    # ── Run gap detection ──────────────────────────────────────────────────────
    col_run, col_info = st.columns([2, 5])
    if col_run.button("🔍 Scan for New Gaps", type="primary", key="gap_scan_btn"):
        with st.spinner("Scanning all sequences..."):
            results = _q(
                "SELECT * FROM detect_all_doc_gaps(%(fy)s, %(by)s)",
                {
                    "fy": datetime.date.today().strftime("%y%m"),
                    "by": st.session_state.get("user_name", "staff"),
                }
            ) or []
        total = sum(int(r.get("gaps_found") or 0) for r in results)
        if total:
            st.warning(f"⚠️ {total} new gap(s) detected across document sequences.")
        else:
            st.success("✅ No new gaps found — all sequences are clean.")
        st.rerun()

    # ── Filter controls ────────────────────────────────────────────────────────
    with st.expander("🔍 Filters", expanded=True):
        fc1, fc2, fc3 = st.columns(3)
        doc_types = fc1.multiselect(
            "Document Type",
            ["ORDER", "CHALLAN", "INVOICE", "DISPATCH", "PAYMENT", "CN", "DN"],
            default=["ORDER", "CHALLAN", "INVOICE", "DISPATCH", "CN", "DN"],
            key="gap_doc_type",
        )
        statuses = fc2.multiselect(
            "Status",
            ["UNEXPLAINED", "EXPLAINED", "VOIDED", "TEST_DATA", "SYSTEM_ERROR"],
            default=["UNEXPLAINED"],
            key="gap_status",
        )
        show_resolved = fc3.checkbox("Show resolved", value=False, key="gap_show_resolved")

    # ── Load gaps ──────────────────────────────────────────────────────────────
    rows = _q("""
        SELECT id::text, doc_type, doc_prefix, fy,
               gap_from, gap_to, gap_count,
               status, reason, detected_at::text AS detected_at,
               detected_by, resolution_note, resolved_at::text AS resolved_at
        FROM doc_number_gap_log
        WHERE (%(types)s = '{}' OR doc_type = ANY(%(types)s))
          AND (%(statuses)s = '{}' OR status = ANY(%(statuses)s))
          AND (%(show_res)s OR resolved_at IS NULL)
        ORDER BY detected_at DESC
        LIMIT 200
    """, {
        "types":    doc_types or [],
        "statuses": statuses or ["UNEXPLAINED"],
        "show_res": show_resolved,
    })

    if not rows:
        st.success("✅ No gaps found for the selected filters.")
        return

    # ── Summary metrics ────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Gaps",       len(rows))
    m2.metric("Unexplained",      sum(1 for r in rows if r["status"] == "UNEXPLAINED"))
    m3.metric("Numbers Missing",  sum(int(r.get("gap_count") or 0) for r in rows))
    m4.metric("Explained",        sum(1 for r in rows if r["status"] != "UNEXPLAINED"))
    st.markdown("---")

    # ── Per-gap cards ──────────────────────────────────────────────────────────
    STATUS_COLORS = {
        "UNEXPLAINED":  "#ef4444",
        "EXPLAINED":    "#10b981",
        "VOIDED":       "#6366f1",
        "TEST_DATA":    "#64748b",
        "SYSTEM_ERROR": "#f59e0b",
    }

    for row in rows:
        rid        = row["id"]
        doc_type   = row["doc_type"]
        prefix     = row["doc_prefix"]
        fy         = row["fy"]
        gap_from   = int(row["gap_from"])
        gap_to     = int(row["gap_to"])
        gap_count  = int(row.get("gap_count") or 1)
        status     = row["status"]
        reason     = row.get("reason") or ""
        detected   = str(row.get("detected_at") or "")[:16]
        res_note   = row.get("resolution_note") or ""

        # Build gap range label
        pad = 4 if doc_type != "DISPATCH" else 5
        gap_label = f"{prefix}/{fy}/{str(gap_from).zfill(pad)}"
        if gap_to > gap_from:
            gap_label += f" → {prefix}/{fy}/{str(gap_to).zfill(pad)} ({gap_count} numbers)"

        color = STATUS_COLORS.get(status, "#64748b")

        with st.container(border=True):
            c1, c2, c3 = st.columns([5, 2, 2])
            with c1:
                st.markdown(
                    f"<div style='font-size:0.85rem;font-weight:700;color:#e2e8f0'>"
                    f"🔢 {gap_label}</div>"
                    f"<div style='font-size:0.72rem;color:#94a3b8'>"
                    f"Detected: {detected}  ·  By: {row.get('detected_by','system')}</div>"
                    + (f"<div style='font-size:0.72rem;color:#64748b'>Note: {reason}</div>"
                       if reason else ""),
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(
                    f"<span style='background:{color}22;border:1px solid {color}55;"
                    f"color:{color};padding:3px 10px;border-radius:10px;"
                    f"font-size:0.72rem;font-weight:700'>{status}</span>",
                    unsafe_allow_html=True,
                )
            with c3:
                st.caption(f"{doc_type} · {gap_count} number{'s' if gap_count>1 else ''}")

            # Resolution form
            if status == "UNEXPLAINED":
                with st.expander("📝 Explain this gap", expanded=False):
                    _new_status = st.selectbox(
                        "Reason",
                        ["EXPLAINED", "VOIDED", "TEST_DATA", "SYSTEM_ERROR"],
                        key=f"gap_st_{rid}",
                    )
                    _note = st.text_input(
                        "Details",
                        placeholder="e.g. Customer cancelled at punching, test entry, session crash…",
                        key=f"gap_note_{rid}",
                    )
                    if st.button("✅ Save", key=f"gap_save_{rid}", type="primary"):
                        if _rw("""
                            UPDATE doc_number_gap_log
                            SET status          = %(s)s,
                                resolution_note = %(n)s,
                                resolved_at     = NOW(),
                                resolved_by     = %(by)s
                            WHERE id = %(id)s::uuid
                        """, {
                            "s":   _new_status,
                            "n":   _note.strip(),
                            "by":  st.session_state.get("user_name", "staff"),
                            "id":  rid,
                        }):
                            st.success("✅ Gap resolved")
                            st.rerun()
            elif res_note:
                st.caption(f"✅ Resolution: {res_note}")
