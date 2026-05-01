"""
modules/admin/audit_log_ui.py
==============================
Audit Log Dashboard — view financial audit trail, detect anomalies.

Tabs:
  1. 🔴 High Risk Actions  — reversals, deletions
  2. 🟡 Frequent Edits     — records edited > 3 times (suspicious)
  3. 👤 User Activity      — who did what, how many actions
  4. 🔍 Change Diff        — old vs new value for any record
  5. 📋 Full Log           — all events searchable
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _date_filter(key="al"):
    presets = {
        "Today":      (date.today(), date.today()),
        "Last 7 days":(date.today() - timedelta(days=7), date.today()),
        "This month": (date.today().replace(day=1), date.today()),
        "All time":   (date(2020, 1, 1), date.today()),
    }
    c1, c2, c3 = st.columns([1, 1, 1])
    preset = c3.selectbox("Period", list(presets.keys()), key=f"{key}_pre")
    fd, td = presets[preset]
    fd = c1.date_input("From", value=fd, key=f"{key}_fd")
    td = c2.date_input("To",   value=td, key=f"{key}_td")
    return fd, td


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — HIGH RISK ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _tab_high_risk():
    st.caption("Reversals, deletions and high-value actions — review these carefully")

    fd, td = _date_filter("hr")

    rows = _q("""
        SELECT
            created_at::text        AS "Time",
            event                   AS "Action",
            entity                  AS "Entity",
            entity_id               AS "Entity ID",
            user_id                 AS "User",
            ROUND(COALESCE((payload->>'amount')::numeric, 0), 2) AS "Amount (₹)",
            COALESCE(payload->>'ref_no', '') AS "Ref No"
        FROM audit_log
        WHERE created_at::date BETWEEN %s AND %s
          AND (
            event ILIKE '%reverse%'
            OR event ILIKE '%delete%'
            OR event ILIKE '%void%'
            OR event ILIKE '%cancel%'
            OR event = 'payment_reversed'
            OR event = 'invoice_deleted'
          )
        ORDER BY created_at DESC
        LIMIT 200
    """, (fd, td))

    if not rows:
        st.success("✅ No high-risk actions in this period.")
        return

    df = _df(rows)
    if "Amount (₹)" in df.columns:
        df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    # Alert: reversals > ₹50,000
    high_val = df[df["Amount (₹)"] > 50000] if "Amount (₹)" in df.columns else pd.DataFrame()
    if not high_val.empty:
        st.error(f"🚨 {len(high_val)} HIGH-VALUE reversals (> ₹50,000) detected!")
        st.dataframe(high_val, width='stretch', hide_index=True,
            column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})
        st.markdown("---")

    c1, c2, c3 = st.columns(3)
    c1.metric("High-Risk Actions", len(df))
    c2.metric("Total Value",       f"₹{df['Amount (₹)'].sum():,.0f}" if "Amount (₹)" in df.columns else "—")
    c3.metric("Unique Users",      df["User"].nunique() if "User" in df.columns else "—")

    st.dataframe(df, width='stretch', hide_index=True,
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})

    st.download_button("⬇ Export", df.to_csv(index=False).encode(),
                       file_name="high_risk_actions.csv", key="hr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — FREQUENT EDITS (suspicious activity)
# ══════════════════════════════════════════════════════════════════════════════

def _tab_frequent_edits():
    st.caption("Records edited more than 3 times — may indicate unstable data or suspicious activity")

    fd, td = _date_filter("fe")
    threshold = st.number_input("Edit count threshold", min_value=2, value=3, step=1, key="fe_thresh")

    rows = _q("""
        SELECT
            entity                      AS "Entity",
            entity_id                   AS "Entity ID",
            COUNT(*)                    AS "Edit Count",
            MIN(created_at)::text       AS "First Edit",
            MAX(created_at)::text       AS "Last Edit",
            COUNT(DISTINCT user_id)     AS "Users Involved",
            STRING_AGG(DISTINCT user_id, ', ') AS "Who"
        FROM audit_log
        WHERE created_at::date BETWEEN %s AND %s
          AND event ILIKE '%update%'
        GROUP BY entity, entity_id
        HAVING COUNT(*) > %s
        ORDER BY COUNT(*) DESC
        LIMIT 100
    """, (fd, td, threshold))

    if not rows:
        st.success(f"✅ No records edited more than {threshold} times.")
        return

    df = _df(rows)
    st.warning(f"⚠️ {len(df)} records edited more than {threshold} times")

    for _, row in df.iterrows():
        with st.expander(
            f"⚠️ {row.get('Entity','?')} · {row.get('Entity ID','?')} — "
            f"{row.get('Edit Count','?')} edits by {row.get('Who','?')}",
            expanded=True
        ):
            c1, c2, c3 = st.columns(3)
            c1.metric("Edits",          row.get("Edit Count"))
            c2.metric("Users Involved", row.get("Users Involved"))
            c3.metric("Last Edit",      str(row.get("Last Edit",""))[:16])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — USER ACTIVITY
# ══════════════════════════════════════════════════════════════════════════════

def _tab_user_activity():
    st.caption("Who did what — actions per user")

    fd, td = _date_filter("ua")

    rows = _q("""
        SELECT
            COALESCE(user_id, 'unknown')    AS "User",
            COUNT(*)                        AS "Total Actions",
            COUNT(CASE WHEN event ILIKE '%create%' OR event ILIKE '%save%' THEN 1 END) AS "Creates",
            COUNT(CASE WHEN event ILIKE '%update%' THEN 1 END)   AS "Edits",
            COUNT(CASE WHEN event ILIKE '%delete%' OR event ILIKE '%reverse%' THEN 1 END) AS "Deletes/Rev",
            MIN(created_at)::text           AS "First Action",
            MAX(created_at)::text           AS "Last Action"
        FROM audit_log
        WHERE created_at::date BETWEEN %s AND %s
        GROUP BY user_id
        ORDER BY COUNT(*) DESC
    """, (fd, td))

    if not rows:
        st.info("No activity in this period.")
        return

    df = _df(rows)

    # Summary cards
    cols = st.columns(min(len(df), 4))
    for i, row in df.iterrows():
        if i >= 4: break
        with cols[i]:
            st.markdown(
                f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                f"border-radius:8px;padding:10px 12px;text-align:center'>"
                f"<div style='color:#60a5fa;font-weight:700'>👤 {row.get('User','?')}</div>"
                f"<div style='color:#e2e8f0;font-size:1.2rem;font-weight:700'>"
                f"{row.get('Total Actions',0)}</div>"
                f"<div style='color:#475569;font-size:.7rem'>actions today</div>"
                f"<div style='color:#94a3b8;font-size:.65rem;margin-top:4px'>"
                f"✏️ {row.get('Edits',0)}  🗑️ {row.get('Deletes/Rev',0)}</div>"
                f"</div>",
                unsafe_allow_html=True
            )

    st.markdown("---")
    st.dataframe(df, width='stretch', hide_index=True)
    st.download_button("⬇ Export", df.to_csv(index=False).encode(),
                       file_name="user_activity.csv", key="ua_dl")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — CHANGE DIFF VIEW
# ══════════════════════════════════════════════════════════════════════════════

def _tab_change_diff():
    st.caption("See exactly what changed on any record — field by field before vs after")

    col1, col2 = st.columns(2)
    entity_type = col1.selectbox("Entity Type",
                                  ["invoice","payment","order","challan","stock","journal"],
                                  key="cd_entity")
    entity_id   = col2.text_input("Entity ID / Ref No", key="cd_id",
                                   placeholder="e.g. INV/2526/0001")

    if not entity_id.strip():
        st.info("Enter an entity ID to see its change history.")
        return

    rows = _q("""
        SELECT
            created_at::text    AS "Time",
            event               AS "Action",
            user_id             AS "User",
            payload             AS payload_raw
        FROM audit_log
        WHERE entity = %s
          AND (entity_id = %s OR payload->>'ref_no' = %s)
        ORDER BY created_at ASC
    """, (entity_type, entity_id.strip(), entity_id.strip()))

    if not rows:
        # Try broader search
        rows = _q("""
            SELECT created_at::text AS "Time", event AS "Action",
                   user_id AS "User", payload AS payload_raw
            FROM audit_log
            WHERE payload::text ILIKE %s
            ORDER BY created_at ASC LIMIT 20
        """, (f"%{entity_id.strip()}%",))

    if not rows:
        st.info(f"No audit entries found for {entity_type} '{entity_id}'.")
        return

    import json
    st.markdown(f"**{len(rows)} audit events for {entity_type} `{entity_id}`**")

    for row in rows:
        time_str   = str(row.get("Time",""))[:16]
        action     = row.get("Action","")
        user       = row.get("User","system")
        payload_raw = row.get("payload_raw") or {}

        if isinstance(payload_raw, str):
            try: payload_raw = json.loads(payload_raw)
            except: payload_raw = {}

        diff = payload_raw.get("diff", {})
        old  = payload_raw.get("old",  {})
        new  = payload_raw.get("new",  {})

        with st.expander(f"**{time_str}** — {action} by {user}", expanded=bool(diff)):
            if diff:
                st.markdown("**Changed fields:**")
                diff_rows = [
                    {"Field": k, "Before": str(v.get("old","")), "After": str(v.get("new",""))}
                    for k, v in diff.items()
                ]
                st.dataframe(_df(diff_rows), width='stretch', hide_index=True)
            elif old or new:
                c1, c2 = st.columns(2)
                c1.markdown("**Before:**")
                c1.json(old)
                c2.markdown("**After:**")
                c2.json(new)
            else:
                st.json(payload_raw)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — FULL LOG
# ══════════════════════════════════════════════════════════════════════════════

def _tab_full_log():
    st.caption("Complete audit trail — searchable by user, entity, action")

    fd, td     = _date_filter("fl")
    search     = st.text_input("🔍 Search (user, entity ID, action)", key="fl_search")
    event_filter = st.selectbox("Action type",
                                 ["All","invoice_created","invoice_updated","payment_created",
                                  "payment_reversed","stock_adjusted","save_order","status_changed"],
                                 key="fl_event")

    sf = f"AND payload::text ILIKE %(search)s" if search else ""
    ef = f"AND event = %(event)s" if event_filter != "All" else ""

    rows = _q(f"""
        SELECT
            created_at::text    AS "Time",
            event               AS "Action",
            entity              AS "Entity",
            entity_id           AS "Entity ID",
            user_id             AS "User",
            COALESCE(payload->>'ref_no','') AS "Ref",
            ROUND(COALESCE((payload->>'amount')::numeric,0),2) AS "Amount (₹)"
        FROM audit_log
        WHERE created_at::date BETWEEN %(fd)s AND %(td)s
          {sf} {ef}
        ORDER BY created_at DESC
        LIMIT 500
    """, {"fd": fd, "td": td,
          "search": f"%{search}%",
          "event":  event_filter})

    if not rows:
        st.info("No entries found.")
        return

    df = _df(rows)
    if "Amount (₹)" in df.columns:
        df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    st.caption(f"{len(df)} entries")
    st.dataframe(df, width='stretch', hide_index=True,
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})

    st.download_button("⬇ Export Full Log",
        df.to_csv(index=False).encode(),
        file_name=f"audit_log_{fd}_{td}.csv", key="fl_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def render_audit_log():
    st.markdown("## 🔍 Audit Log")

    # Permission check
    try:
        from modules.security.module_permissions import require_permission
        require_permission("view_audit_log")
    except ImportError:
        pass  # permissions module not yet deployed

    tabs = st.tabs([
        "🔴 High Risk Actions",
        "🟡 Frequent Edits",
        "👤 User Activity",
        "🔍 Change Diff",
        "📋 Full Log",
    ])

    with tabs[0]: _tab_high_risk()
    with tabs[1]: _tab_frequent_edits()
    with tabs[2]: _tab_user_activity()
    with tabs[3]: _tab_change_diff()
    with tabs[4]: _tab_full_log()
