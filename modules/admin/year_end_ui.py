"""
year_end_ui.py
Admin-only UI for financial year management.
Located in Settings → ⚙️ Year-End Management.
"""
import streamlit as st
import datetime


def render_year_end_management():
    """Main entry point — call from settings/admin page."""
    _check_admin_only()
    _render_header()
    _render_current_fy()
    st.markdown("---")
    _render_all_years()


# ─────────────────────────────────────────────────────────────────────────────
def _check_admin_only():
    role = st.session_state.get("user_role", "STAFF")
    if role.upper() != "ADMIN":
        st.error("⛔ Year-End Management is restricted to ADMIN only.")
        st.stop()


def _render_header():
    st.markdown(
        "<div style='background:#0f172a;border-left:4px solid #f59e0b;"
        "padding:10px 16px;border-radius:0 8px 8px 0;margin-bottom:16px'>"
        "<b style='color:#f59e0b;font-size:1rem'>⚙️ Financial Year Management</b>"
        "<span style='color:#78350f;font-size:0.8rem;margin-left:10px'>"
        "Admin only · All actions are irreversible</span>"
        "</div>",
        unsafe_allow_html=True
    )


def _render_current_fy():
    from modules.services.fy_service import get_current_fy, ensure_fy_seeded
    ensure_fy_seeded()
    fy = get_current_fy()

    is_closed = bool(fy.get("is_closed"))

    # Status banner
    if is_closed:
        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
            f"border-radius:8px;padding:10px 16px;margin-bottom:12px'>"
            f"<span style='color:#ef4444;font-weight:700'>🔒 CLOSED</span>"
            f"<span style='color:#94a3b8;font-size:.85rem;margin-left:10px'>"
            f"FY {fy['fy']} · Closed on {str(fy.get('closed_at',''))[:10]}"
            f" by {fy.get('closed_by','')}</span>"
            f"</div>",
            unsafe_allow_html=True
        )
        return

    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
        f"border-radius:8px;padding:10px 16px;margin-bottom:12px'>"
        f"<span style='color:#10b981;font-weight:700'>🟢 ACTIVE</span>"
        f"<span style='color:#94a3b8;font-size:.85rem;margin-left:10px'>"
        f"FY {fy['fy']} · {fy['start_date']} → {fy['end_date']}</span>"
        f"</div>",
        unsafe_allow_html=True
    )

    # Year summary
    _render_fy_summary(fy["fy_short"])

    st.markdown("---")

    # Close FY section
    st.markdown("### 🔒 Close Financial Year")
    st.warning(
        f"**This will:**\n"
        f"- Lock all records in FY {fy['fy']} — no edits allowed after close\n"
        f"- Calculate closing balances and post opening entries for next FY\n"
        f"- Reset Challan / Invoice / Payment / JV counters to 0\n"
        f"- Orders and Consultations continue from their current number\n"
        f"- Create FY {_next_fy_label(fy['end_date'])} automatically"
    )

    _confirm = st.checkbox(
        f"I confirm I want to close FY {fy['fy']} — this cannot be undone",
        key="fy_close_confirm"
    )

    if st.button(
        f"🔒 Close FY {fy['fy']}",
        type="primary",
        disabled=not _confirm,
        key="btn_close_fy"
    ):
        _do_close_fy(fy)


def _do_close_fy(fy: dict):
    closed_by = st.session_state.get("user_name", "ADMIN")
    with st.spinner(f"Closing FY {fy['fy']}… please wait"):
        try:
            from modules.services.year_end_service import close_financial_year
            result = close_financial_year(closed_by=closed_by)

            if "error" in result:
                st.error(f"❌ {result['error']}")
                return

            st.success(f"✅ FY {result['closed_fy']} closed successfully!")
            st.balloons()

            col1, col2, col3 = st.columns(3)
            col1.metric("Opening entries posted", result["ob_entries"])
            col2.metric("Counters reset",         result["reset_series"])
            col3.metric("Next FY",                result["next_fy"])

            st.info(
                f"Next FY **{result['next_fy']}** starts {result['next_start']}. "
                f"All new Challans/Invoices/Payments will start from 0001."
            )

        except Exception as e:
            st.error(f"❌ Year-end close failed: {e}")


def _render_fy_summary(fy_short: str):
    """Quick stats for the current FY."""
    try:
        from modules.sql_adapter import run_query as _rq

        orders = (_rq(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_value),0) AS val "
            "FROM orders WHERE fy=%s AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)
        ) or [{}])[0]

        invoices = (_rq(
            "SELECT COUNT(*) AS n, COALESCE(SUM(grand_total),0) AS val "
            "FROM invoices WHERE fy=%s AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)
        ) or [{}])[0]

        payments = (_rq(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS val "
            "FROM payments WHERE fy=%s "
            "AND payment_type IN ('PAYMENT','RECEIPT','ADVANCE') "
            "AND COALESCE(is_deleted,FALSE)=FALSE",
            (fy_short,)
        ) or [{}])[0]

        c1, c2, c3 = st.columns(3)
        c1.metric("Orders this FY",   int(orders.get("n") or 0),
                  f"₹{float(orders.get('val') or 0):,.0f}")
        c2.metric("Invoices this FY", int(invoices.get("n") or 0),
                  f"₹{float(invoices.get('val') or 0):,.0f}")
        c3.metric("Payments this FY", int(payments.get("n") or 0),
                  f"₹{float(payments.get('val') or 0):,.0f}")
    except Exception:
        pass


def _render_all_years():
    """Table of all financial years with status."""
    st.markdown("### 📅 All Financial Years")
    try:
        from modules.services.fy_service import all_financial_years
        years = all_financial_years()

        if not years:
            st.info("No financial year records found.")
            return

        for fy in years:
            is_closed = bool(fy.get("is_closed"))
            badge = "🔒 CLOSED" if is_closed else "🟢 ACTIVE"
            color = "#ef4444" if is_closed else "#10b981"
            closed_info = ""
            if is_closed and fy.get("closed_at"):
                closed_info = (f" · Closed {str(fy['closed_at'])[:10]}"
                               f" by {fy.get('closed_by','')}")

            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                f"border-radius:6px;padding:8px 14px;margin:4px 0;"
                f"display:flex;align-items:center;gap:16px'>"
                f"<span style='color:{color};font-weight:700;min-width:90px'>{badge}</span>"
                f"<span style='color:#e2e8f0;font-weight:600'>{fy['fy']}</span>"
                f"<span style='color:#475569;font-size:.8rem'>"
                f"{fy['start_date']} → {fy['end_date']}{closed_info}</span>"
                f"</div>",
                unsafe_allow_html=True
            )

            # Previous year report button
            if is_closed:
                if st.button(
                    f"📊 View {fy['fy']} Summary",
                    key=f"fy_report_{fy['fy']}",
                    use_container_width=False
                ):
                    _render_closed_fy_report(fy)

    except Exception as e:
        st.error(f"Error loading financial years: {e}")


def _render_closed_fy_report(fy: dict):
    """Expandable report for a closed FY."""
    with st.expander(f"📋 FY {fy['fy']} Report", expanded=True):
        try:
            from modules.services.year_end_service import get_year_summary
            summary = get_year_summary(fy["fy"])
            if not summary:
                st.info("No data found for this FY.")
                return
            c1, c2, c3 = st.columns(3)
            c1.metric("Orders",   int(summary["orders"].get("n") or 0),
                      f"₹{float(summary['orders'].get('val') or 0):,.0f}")
            c2.metric("Invoices", int(summary["invoices"].get("n") or 0),
                      f"₹{float(summary['invoices'].get('val') or 0):,.0f}")
            c3.metric("Payments", int(summary["payments"].get("n") or 0),
                      f"₹{float(summary['payments'].get('val') or 0):,.0f}")
        except Exception as e:
            st.error(f"Report error: {e}")


def _next_fy_label(end_date) -> str:
    """Given end_date of current FY, return label of next FY."""
    if isinstance(end_date, str):
        end_date = datetime.date.fromisoformat(end_date)
    next_start = datetime.date(end_date.year, 4, 1)
    if next_start.month >= 4:
        return f"{next_start.year}-{str(next_start.year+1)[-2:]}"
    return f"{next_start.year-1}-{str(next_start.year)[-2:]}"
