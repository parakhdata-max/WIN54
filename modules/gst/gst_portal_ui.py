"""
GST Portal skeleton.

First version is read-only and export-oriented. Later API/GSP upload can use the
same prepared data after CA validation and GST portal credential setup.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Dict, List

import pandas as pd
import streamlit as st

from modules.sql_adapter import run_query


def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    rows = run_query(sql, params or {})
    df = pd.DataFrame(rows or [])
    return df.where(pd.notnull(df), None)


def _month_bounds(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _download_excel(label: str, sheets: Dict[str, pd.DataFrame], filename: str) -> None:
    buf = BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for sheet, df in sheets.items():
                clean = df.copy() if df is not None else pd.DataFrame()
                clean.to_excel(writer, index=False, sheet_name=sheet[:31])
        st.download_button(
            label,
            data=buf.getvalue(),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    except Exception as exc:
        st.warning(f"Excel export unavailable: {exc}")


def _download_csv(label: str, df: pd.DataFrame, filename: str) -> None:
    st.download_button(
        label,
        data=(df if df is not None else pd.DataFrame()).to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
    )


def _sales_lines(start: date, end: date) -> pd.DataFrame:
    return _q(
        """
        SELECT
            i.invoice_no                         AS "Invoice No",
            i.invoice_date                       AS "Invoice Date",
            COALESCE(p.party_name, 'Unknown')    AS "Party",
            COALESCE(p.gstin, '')                AS "GSTIN",
            COALESCE(p.state_code, '')           AS "State Code",
            COALESCE(p.state_name, '')           AS "Place of Supply",
            CASE WHEN COALESCE(p.gstin, '') <> '' THEN 'B2B' ELSE 'B2C' END AS "GST Section",
            COALESCE(il.product_name, 'Line')    AS "Product",
            ''                                   AS "HSN/SAC",
            COALESCE(il.quantity, 1)             AS "Qty",
            COALESCE(il.unit_price, 0)           AS "Unit Price",
            COALESCE(il.total_price, 0)          AS "Taxable Value",
            COALESCE(il.gst_percent, il.tax_rate, 0) AS "GST Rate",
            CASE
              WHEN COALESCE(p.state_code, '') = '27' OR COALESCE(p.state_code, '') = ''
              THEN ROUND(COALESCE(il.tax_amount, i.total_tax, 0) / NULLIF(COUNT(*) OVER (PARTITION BY i.id),0) / 2, 2)
              ELSE 0
            END AS "CGST",
            CASE
              WHEN COALESCE(p.state_code, '') = '27' OR COALESCE(p.state_code, '') = ''
              THEN ROUND(COALESCE(il.tax_amount, i.total_tax, 0) / NULLIF(COUNT(*) OVER (PARTITION BY i.id),0) / 2, 2)
              ELSE 0
            END AS "SGST",
            CASE
              WHEN COALESCE(p.state_code, '') <> '' AND COALESCE(p.state_code, '') <> '27'
              THEN ROUND(COALESCE(il.tax_amount, i.total_tax, 0) / NULLIF(COUNT(*) OVER (PARTITION BY i.id),0), 2)
              ELSE 0
            END AS "IGST",
            COALESCE(il.line_total, 0)           AS "Line Total",
            COALESCE(i.grand_total, 0)           AS "Invoice Total",
            COALESCE(i.tally_synced, FALSE)      AS "External Synced"
        FROM invoices i
        LEFT JOIN invoice_lines il ON il.invoice_id = i.id AND COALESCE(il.is_deleted, FALSE) = FALSE
        LEFT JOIN parties p ON p.id = i.party_id
        WHERE COALESCE(i.is_deleted, FALSE) = FALSE
          AND COALESCE(i.status, '') NOT IN ('CANCELLED', 'VOID')
          AND i.invoice_date >= %(start)s
          AND i.invoice_date < %(end)s
        ORDER BY i.invoice_date, i.invoice_no, il.id
        """,
        {"start": start, "end": end},
    )


def _sales_summary(lines: pd.DataFrame) -> pd.DataFrame:
    if lines.empty:
        return pd.DataFrame(columns=["GST Section", "GST Rate", "Taxable Value", "CGST", "SGST", "IGST", "Line Total"])
    numeric_cols = ["Taxable Value", "CGST", "SGST", "IGST", "Line Total"]
    work = lines.copy()
    for col in numeric_cols:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)
    return (
        work.groupby(["GST Section", "GST Rate"], dropna=False)[numeric_cols]
        .sum()
        .reset_index()
        .sort_values(["GST Section", "GST Rate"])
    )


def _notes(note_type: str, start: date, end: date) -> pd.DataFrame:
    table = "credit_notes" if note_type == "Credit" else "debit_notes"
    no_col = "cn_number" if note_type == "Credit" else "dn_number"
    date_col = "cn_date" if note_type == "Credit" else "dn_date"
    return _q(
        f"""
        SELECT
            {no_col}                         AS "Note No",
            {date_col}                       AS "Note Date",
            invoice_no                       AS "Original Invoice",
            original_invoice_date            AS "Original Invoice Date",
            COALESCE(party_name, 'Unknown')  AS "Party",
            COALESCE(party_gstin, '')        AS "GSTIN",
            COALESCE(place_of_supply, '')    AS "Place of Supply",
            COALESCE(supply_type, '')        AS "Supply Type",
            COALESCE(reason, '')             AS "Reason",
            COALESCE(taxable_amount, 0)      AS "Taxable Value",
            COALESCE(cgst_amount, 0)         AS "CGST",
            COALESCE(sgst_amount, 0)         AS "SGST",
            COALESCE(igst_amount, 0)         AS "IGST",
            COALESCE(total_tax_amount, 0)    AS "Total Tax",
            COALESCE(grand_total, 0)         AS "Grand Total",
            COALESCE(status, '')             AS "Status"
        FROM {table}
        WHERE COALESCE(is_deleted, FALSE) = FALSE
          AND {date_col} >= %(start)s
          AND {date_col} < %(end)s
        ORDER BY {date_col}, {no_col}
        """,
        {"start": start, "end": end},
    )


def _missing_master_checks(lines: pd.DataFrame) -> List[str]:
    issues: List[str] = []
    if lines.empty:
        return ["No sales invoices found for selected period."]
    if (lines["GSTIN"].fillna("").eq("") & lines["GST Section"].eq("B2B")).any():
        issues.append("Some B2B rows have missing GSTIN.")
    if lines["HSN/SAC"].fillna("").eq("").any():
        issues.append("HSN/SAC is blank in this skeleton. Map product/main group HSN before final portal upload.")
    if lines["Place of Supply"].fillna("").eq("").any():
        issues.append("Some invoices have missing place of supply/state.")
    if pd.to_numeric(lines["GST Rate"], errors="coerce").isna().any():
        issues.append("Some invoice lines have missing GST rate.")
    return issues


def _render_overview() -> None:
    st.subheader("How GST Upload Usually Works")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Manual click flow**")
        st.write("Most accounting software prepares GSTR data, validates it, exports Excel/JSON, then the operator uploads it on the GST portal.")
        st.write("This is safest for first deployment because CA review happens before portal submission.")
    with c2:
        st.markdown("**Auto/API flow**")
        st.write("Larger systems connect through GST Suvidha Provider/API, generate OTP/auth token, push invoices/returns, and keep acknowledgement status.")
        st.write("This needs credentials, API provider, error reconciliation, and strict locking after upload.")

    st.info(
        "Recommended path here: start manual export + validation now. Later add API upload using the same prepared datasets."
    )

    st.markdown("**Build Checklist**")
    checklist = [
        "Confirm shop GSTIN, legal name, state code and filing frequency.",
        "Map HSN/SAC by product group and service charge type.",
        "Validate B2B GSTIN and place of supply before GSTR-1 export.",
        "Finalize invoice lock rule after portal/Tally/GST upload.",
        "Add credit/debit note amendment tracking and original invoice mapping.",
        "Add GSTR-2B import/reconciliation against purchase invoices.",
        "Add JSON schema generation for GST portal offline utility.",
        "Add API/GSP connector only after manual export is stable.",
        "Store upload batch id, acknowledgement/reference id and uploaded_by.",
        "Create mismatch report: ERP vs GST portal vs Tally.",
    ]
    for item in checklist:
        st.checkbox(item, value=False, disabled=True, key=f"gst_check_{item}")


def _render_gstr1(start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    st.subheader("GSTR-1 Skeleton")
    lines = _sales_lines(start, end)
    summary = _sales_summary(lines)
    cn = _notes("Credit", start, end)
    dn = _notes("Debit", start, end)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Invoice Lines", len(lines))
    k2.metric("Sales Taxable", f"Rs. {pd.to_numeric(lines.get('Taxable Value', pd.Series(dtype=float)), errors='coerce').fillna(0).sum():,.2f}")
    k3.metric("Credit Notes", len(cn))
    k4.metric("Debit Notes", len(dn))

    issues = _missing_master_checks(lines)
    if issues:
        with st.expander("Validation checklist for selected period", expanded=True):
            for issue in issues:
                st.warning(issue)
    else:
        st.success("Basic skeleton validation passed.")

    tab1, tab2, tab3, tab4 = st.tabs(["Sales Lines", "Summary", "Credit Notes", "Debit Notes"])
    with tab1:
        st.dataframe(lines, use_container_width=True, hide_index=True)
        _download_csv("Download Sales CSV", lines, "gst_sales_lines.csv")
    with tab2:
        st.dataframe(summary, use_container_width=True, hide_index=True)
    with tab3:
        st.dataframe(cn, use_container_width=True, hide_index=True)
    with tab4:
        st.dataframe(dn, use_container_width=True, hide_index=True)

    _download_excel(
        "Download GSTR-1 Skeleton Workbook",
        {"Sales_Lines": lines, "Summary": summary, "Credit_Notes": cn, "Debit_Notes": dn},
        "gstr1_skeleton.xlsx",
    )
    return lines, summary, cn, dn


def _render_gstr2b() -> None:
    st.subheader("GSTR-2B Reconciliation Skeleton")
    st.caption("Later this will compare GST portal 2B supplier data against purchase invoices in ERP.")
    up = st.file_uploader("Upload GSTR-2B CSV/XLSX later", type=["csv", "xlsx"], key="gst_2b_upload")
    if up is None:
        st.info("Skeleton only: upload parser and purchase reconciliation will be added after purchase invoice fields are finalized.")
        return
    try:
        if up.name.lower().endswith(".csv"):
            df = pd.read_csv(up)
        else:
            df = pd.read_excel(up)
        st.dataframe(df.head(200), use_container_width=True, hide_index=True)
        st.success("File preview loaded. Reconciliation rules are pending.")
    except Exception as exc:
        st.error(f"Could not preview file: {exc}")


def _render_upload_control() -> None:
    st.subheader("Upload Control Skeleton")
    mode = st.radio("Upload mode", ["Manual Export", "API / GSP Later", "Both"], horizontal=True)
    if mode in {"Manual Export", "Both"}:
        st.markdown("**Manual path**")
        st.write("1. Prepare GSTR-1 workbook/JSON.")
        st.write("2. CA/admin validates GSTIN, HSN, taxable value and notes.")
        st.write("3. Upload on GST portal or offline utility.")
        st.write("4. Mark batch as uploaded and lock invoices.")
    if mode in {"API / GSP Later", "Both"}:
        st.markdown("**Auto path later**")
        st.write("1. Register GST API/GSP credentials.")
        st.write("2. Generate auth token/OTP.")
        st.write("3. Push invoice/note payloads.")
        st.write("4. Save acknowledgement id and per-document errors.")
        st.write("5. Lock uploaded invoices and allow correction only via notes/amendments.")
    st.warning("Live upload is intentionally disabled in this skeleton.")


def render_gst_portal() -> None:
    st.title("GST Portal")
    st.caption("Skeleton for GSTR-1 sales, credit/debit notes, GSTR-2B reconciliation and future GST portal upload.")

    default_start, default_end = _month_bounds()
    c1, c2 = st.columns(2)
    start = c1.date_input("From", value=default_start, key="gst_from")
    end_inclusive = c2.date_input("To", value=default_end, key="gst_to")
    end = end_inclusive
    if start >= end:
        st.error("From date must be before To date.")
        return

    tabs = st.tabs(["Overview", "GSTR-1 Sales", "GSTR-2B", "Upload Control"])
    with tabs[0]:
        _render_overview()
    with tabs[1]:
        _render_gstr1(start, end)
    with tabs[2]:
        _render_gstr2b()
    with tabs[3]:
        _render_upload_control()
