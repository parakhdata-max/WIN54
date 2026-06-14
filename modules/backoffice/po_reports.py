from __future__ import annotations

import datetime

import pandas as pd
import streamlit as st


def _q(sql: str, params: dict | None = None):
    try:
        from modules.sql_adapter import run_query

        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"PO report DB error: {e}")
        return []


def render_po_reports():
    st.markdown("### 📑 Purchase Order Reports")
    st.caption("Internal PO reports grouped by supplier, date, and customer party.")

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        date_from = st.date_input(
            "From",
            value=datetime.date.today() - datetime.timedelta(days=30),
            format="DD/MM/YYYY",
            key="po_rep_from",
        )
    with c2:
        date_to = st.date_input(
            "To",
            value=datetime.date.today(),
            format="DD/MM/YYYY",
            key="po_rep_to",
        )
    with c3:
        supplier_search = st.text_input(
            "Supplier / PO / Party Search",
            placeholder="Search supplier, PO no, party, order no...",
            key="po_rep_search",
        ).strip()

    params = {
        "df": str(date_from),
        "dt": str(date_to),
        "search": f"%{supplier_search.lower()}%",
    }

    search_sql = ""
    if supplier_search:
        search_sql = """
        AND (
            LOWER(COALESCE(so.supplier_name,'')) LIKE %(search)s
            OR LOWER(COALESCE(so.supplier_order_id,'')) LIKE %(search)s
            OR LOWER(COALESCE(o.order_no,'')) LIKE %(search)s
            OR LOWER(COALESCE(o.party_name,'')) LIKE %(search)s
            OR LOWER(COALESCE(o.patient_name,'')) LIKE %(search)s
        )
        """

    base_sql = f"""
        SELECT
            so.id::text AS po_id,
            so.supplier_order_id AS po_no,
            so.supplier_name,
            so.order_date::date AS po_date,
            COALESCE(so.status,'') AS po_status,
            soi.id::text AS po_item_id,
            soi.item_no,
            soi.product_name,
            soi.eye_side,
            soi.ordered_qty,
            soi.unit_price,
            soi.total_price,
            soi.customer_line_id,
            ol.id::text AS order_line_id,
            o.order_no,
            COALESCE(o.party_name, o.patient_name, '—') AS party_name,
            o.id::text AS order_id
        FROM supplier_orders so
        LEFT JOIN supplier_order_items soi
          ON soi.supplier_order_id = so.id
        LEFT JOIN order_lines ol
          ON ol.id::text = NULLIF(soi.customer_line_id::text, '')
        LEFT JOIN orders o
          ON o.id = ol.order_id
        WHERE so.order_date::date BETWEEN %(df)s::date AND %(dt)s::date
        {search_sql}
        ORDER BY so.order_date DESC, so.supplier_name, so.supplier_order_id, soi.item_no
    """

    rows = _q(base_sql, params)
    if not rows:
        st.info("No PO data found for selected filters.")
        return

    df = pd.DataFrame(rows)

    total_po = df["po_no"].nunique() if "po_no" in df else 0
    total_items = len(df)
    linked_items = int(df["order_line_id"].notna().sum()) if "order_line_id" in df else 0
    total_value = float(pd.to_numeric(df.get("total_price", 0), errors="coerce").fillna(0).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("POs", total_po)
    m2.metric("PO Lines", total_items)
    m3.metric("Linked Lines", f"{linked_items}/{total_items}")
    m4.metric("PO Value", f"₹{total_value:,.2f}")

    tab1, tab2, tab3, tab4 = st.tabs([
        "By Supplier",
        "By Date",
        "By Party",
        "PO Drill-down",
    ])

    with tab1:
        st.markdown("#### Supplier-wise PO Summary")
        g = (
            df.groupby(["supplier_name"], dropna=False)
            .agg(
                po_count=("po_no", "nunique"),
                line_count=("po_item_id", "count"),
                order_count=("order_no", "nunique"),
                party_count=("party_name", "nunique"),
                value=("total_price", lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
            )
            .reset_index()
            .sort_values(["value", "line_count"], ascending=False)
        )
        st.dataframe(g, use_container_width=True, hide_index=True)

    with tab2:
        st.markdown("#### Date-wise PO Summary")
        g = (
            df.groupby(["po_date"], dropna=False)
            .agg(
                po_count=("po_no", "nunique"),
                line_count=("po_item_id", "count"),
                order_count=("order_no", "nunique"),
                value=("total_price", lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
            )
            .reset_index()
            .sort_values("po_date", ascending=False)
        )
        st.dataframe(g, use_container_width=True, hide_index=True)

    with tab3:
        st.markdown("#### Party-wise PO Summary")
        g = (
            df.groupby(["party_name"], dropna=False)
            .agg(
                po_count=("po_no", "nunique"),
                supplier_count=("supplier_name", "nunique"),
                order_count=("order_no", "nunique"),
                line_count=("po_item_id", "count"),
                value=("total_price", lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
            )
            .reset_index()
            .sort_values(["value", "line_count"], ascending=False)
        )
        st.dataframe(g, use_container_width=True, hide_index=True)

    with tab4:
        st.markdown("#### PO Drill-down")
        po_options = sorted([x for x in df["po_no"].dropna().unique().tolist() if x])
        selected_po = st.selectbox("Select PO", po_options, key="po_rep_selected_po")

        if selected_po:
            sub = df[df["po_no"] == selected_po].copy()
            header = sub.iloc[0]

            st.markdown(
                f"**PO:** `{header.get('po_no')}`  \n"
                f"**Supplier:** {header.get('supplier_name') or '—'}  \n"
                f"**Date:** {header.get('po_date') or '—'}  \n"
                f"**Status:** {header.get('po_status') or '—'}"
            )

            show_cols = [
                "item_no",
                "product_name",
                "eye_side",
                "ordered_qty",
                "unit_price",
                "total_price",
                "order_no",
                "party_name",
                "customer_line_id",
            ]
            show_cols = [c for c in show_cols if c in sub.columns]
            st.dataframe(sub[show_cols], use_container_width=True, hide_index=True)

            missing = sub[sub["order_line_id"].isna()]
            if not missing.empty:
                st.warning(
                    f"{len(missing)} PO item(s) are not linked to order_lines. "
                    "Check supplier_order_items.customer_line_id."
                )
