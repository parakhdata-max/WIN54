from __future__ import annotations

import datetime as _dt
from collections import defaultdict

import streamlit as st

from modules.backoffice.production_shared import _go_to_billing


def _q(sql: str, params: dict | None = None):
    try:
        from modules.sql_adapter import run_query

        return run_query(sql, params or {}) or []
    except Exception as exc:
        st.error(f"Ready billing DB error: {exc}")
        return []


def _money(v) -> str:
    try:
        return f"₹{float(v or 0):,.2f}"
    except Exception:
        return "₹0.00"


def render_ready_billing_panel() -> None:
    st.markdown("### 💳 Ready Billing")
    st.caption("Orders whose open lines pass the billing gate. Open one order directly to Backoffice Billing Summary.")

    c1, c2, c3 = st.columns([1, 1, 2])
    date_from = c1.date_input(
        "From",
        value=_dt.date.today() - _dt.timedelta(days=30),
        format="DD/MM/YYYY",
        key="rb_from",
    )
    date_to = c2.date_input(
        "To",
        value=_dt.date.today(),
        format="DD/MM/YYYY",
        key="rb_to",
    )
    search = c3.text_input(
        "Search",
        placeholder="Order no / patient / party / product",
        key="rb_search",
    ).strip().lower()

    params = {"df": str(date_from), "dt": str(date_to)}
    search_sql = ""
    if search:
        search_sql = """
        AND (
            LOWER(o.order_no) LIKE %(s)s
            OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(s)s
            OR LOWER(COALESCE(p.product_name,'')) LIKE %(s)s
        )
        """
        params["s"] = f"%{search}%"

    rows = _q(
        f"""
        SELECT
            o.id::text AS order_id,
            o.order_no,
            COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
            o.status AS order_status,
            o.created_at::date AS order_date,
            ol.id::text AS line_id,
            COALESCE(ol.eye_side,'') AS eye_side,
            COALESCE(p.product_name,
                     ol.lens_params->>'service_display_name',
                     ol.lens_params->>'display_product_name',
                     ol.lens_params->>'service_description',
                     'Service') AS product_name,
            COALESCE(ol.quantity, ol.allocated_qty, ol.billed_qty, 1) AS qty,
            COALESCE(ol.billing_total, ol.total_price, ol.unit_price * COALESCE(ol.quantity,1), 0) AS line_total,
            COALESCE(ol.lens_params->>'manufacturing_route','') AS route,
            COALESCE(ol.lens_params->>'supplier_stage','') AS supplier_stage,
            jm.current_stage AS job_stage
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        LEFT JOIN products p ON p.id = ol.product_id
        LEFT JOIN job_master jm ON jm.order_line_id = ol.id
        WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
          AND o.status NOT IN ('CANCELLED','CLOSED')
          AND o.created_at::date BETWEEN %(df)s::date AND %(dt)s::date
          AND NOT EXISTS (
              SELECT 1 FROM challan_lines cl
              JOIN challans c ON c.id = cl.challan_id
              WHERE cl.order_line_id = ol.id
                AND c.status NOT IN ('CANCELLED','VOID')
          )
          {search_sql}
        ORDER BY o.created_at DESC, o.order_no,
                 CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
        LIMIT 500
        """,
        params,
    )
    if not rows:
        st.info("No open unbilled lines found for selected filters.")
        return

    try:
        from modules.billing.challan_invoice_manager import is_line_billing_ready
    except Exception as exc:
        st.error(f"Billing gate unavailable: {exc}")
        return

    grouped: dict[str, dict] = defaultdict(lambda: {"info": {}, "ready": [], "blocked": []})
    for r in rows:
        ready, reason = is_line_billing_ready(str(r.get("line_id") or ""))
        bucket = grouped[str(r["order_id"])]
        bucket["info"] = {
            "order_id": r["order_id"],
            "order_no": r["order_no"],
            "patient_name": r["patient_name"],
            "order_date": r["order_date"],
            "order_status": r["order_status"],
        }
        item = dict(r)
        item["gate_reason"] = reason
        if ready:
            bucket["ready"].append(item)
        else:
            bucket["blocked"].append(item)

    ready_orders = [g for g in grouped.values() if g["ready"] and not g["blocked"]]
    blocked_orders = [g for g in grouped.values() if g["blocked"]]

    m1, m2, m3 = st.columns(3)
    m1.metric("Ready Orders", len(ready_orders))
    m2.metric("Ready Lines", sum(len(g["ready"]) for g in ready_orders))
    m3.metric("Blocked Orders", len(blocked_orders))

    if not ready_orders:
        st.warning("No complete orders are ready to bill. Blocked items are shown below for review.")
    for g in ready_orders:
        info = g["info"]
        total = sum(float(x.get("line_total") or 0) for x in g["ready"])
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1.5, 1.5])
            c1.markdown(
                f"**{info['order_no']}** · {info['patient_name']}  \n"
                f"{info['order_date']} · {len(g['ready'])} line(s)"
            )
            c2.metric("Bill Amount", _money(total))
            if c3.button("💳 Open Billing", key=f"rb_open_{info['order_id']}", use_container_width=True):
                _go_to_billing(info["order_id"], info["order_no"])
            with st.expander("Lines", expanded=False):
                for line in g["ready"]:
                    st.caption(
                        f"{line.get('eye_side') or 'ITEM'} · {line.get('product_name')} · "
                        f"{line.get('route') or '—'} · {_money(line.get('line_total'))}"
                    )

    if blocked_orders:
        with st.expander(f"Blocked / Not Ready ({len(blocked_orders)} order(s))", expanded=False):
            for g in blocked_orders[:50]:
                info = g["info"]
                st.markdown(f"**{info['order_no']}** · {info['patient_name']}")
                for line in g["blocked"]:
                    st.caption(
                        f"{line.get('eye_side') or 'ITEM'} · {line.get('product_name')} · "
                        f"{line.get('gate_reason') or 'Not ready'}"
                    )
