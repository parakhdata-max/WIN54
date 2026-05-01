"""
modules/ui_order_header.py
===========================
Wholesale order header — Party + RoleType + Order details.

Barcode scan support:
  - Scan party barcode/customer_no → auto-fills party + roletype
  - Scan order barcode → loads existing order for edit
  - Scan product barcode → resolves to product (passed to product selector)
"""

import streamlit as st
import datetime
import pandas as pd
from modules.sql_adapter import read_party_master


def render_order_header():

    parties_df = read_party_master(include_inactive=False)

    if parties_df.empty:
        st.error("No parties available — add parties via Data Loader → Party Master first.")
        return None

    st.subheader("📋 Order Information")

    # ── Scanner input — resolves party or order barcode ──────────────────────
    scan_col, clear_col = st.columns([4, 1])
    with scan_col:
        scanned = st.text_input(
            "📷 Scan party card / customer number",
            placeholder="Scan party barcode, CUST000001, or order number",
            key="oh_scanner_input",
            label_visibility="collapsed",
        ).strip().upper()
        if scanned:
            st.session_state["oh_scanner_val"] = scanned
    with clear_col:
        if st.button("✕", key="oh_scanner_clear", use_container_width=True,
                     help="Clear scan"):
            st.session_state.pop("oh_scanner_val", None)
            st.session_state.pop("oh_scanner_input", None)
            st.session_state.pop("oh_scanned_party", None)
            st.rerun()

    scan_val = st.session_state.get("oh_scanner_val", "")

    # Process scan
    if scan_val:
        _process_header_scan(scan_val, parties_df)

    # ── Order fields ──────────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)

    # Pre-fill from scanned party if available
    scanned_party = st.session_state.get("oh_scanned_party")

    with col1:
        role_types = sorted(
            parties_df["party_type"].dropna().astype(str).unique().tolist()
        )
        default_role_idx = 0
        if scanned_party and scanned_party.get("party_type"):
            try:
                default_role_idx = ([""] + role_types).index(scanned_party["party_type"])
            except ValueError:
                default_role_idx = 0

        roletype = st.selectbox(
            "Role Type *",
            [""] + role_types,
            index=default_role_idx,
            key="oh_roletype"
        )

    with col2:
        if roletype:
            filtered = parties_df[
                parties_df["party_type"] == roletype
            ]["party_name"].tolist()
        else:
            filtered = parties_df["party_name"].dropna().tolist()

        filtered_sorted = sorted(filtered)

        # Pre-select scanned party
        default_party_idx = 0
        if scanned_party and scanned_party.get("party_name") in filtered_sorted:
            try:
                default_party_idx = ([""] + filtered_sorted).index(
                    scanned_party["party_name"]
                )
            except ValueError:
                default_party_idx = 0

        party = st.selectbox(
            "Customer / Party *",
            [""] + filtered_sorted,
            index=default_party_idx,
            key="oh_party"
        )

        # Show customer number if party selected
        if party:
            party_row = parties_df[parties_df["party_name"] == party]
            if not party_row.empty:
                cno = party_row.iloc[0].get("customer_no","") or ""
                barcode = party_row.iloc[0].get("barcode","") or ""
                if cno:
                    st.caption(f"Customer#: **{cno}**" + (f" | Barcode: {barcode}" if barcode else ""))

    with col3:
        customer_order_no = st.text_input(
            "Customer Order No",
            key="oh_customer_order_no"
        )

    with col4:
        order_date = st.date_input(
            "Order Date",
            value=datetime.date.today(),
            key="oh_order_date"
        )

    if scanned_party and party == scanned_party.get("party_name"):
        st.success(
            f"✅ Party auto-filled from scan: **{party}** | "
            f"Customer#: {scanned_party.get('customer_no','—')} | "
            f"📞 {scanned_party.get('mobile','—')}"
        )

    st.divider()

    return {
        "party":             party,
        "roletype":          roletype,
        "customer_order_no": customer_order_no,
        "order_date":        order_date,
    }


def _process_header_scan(code: str, parties_df: pd.DataFrame):
    """
    Resolve scanned code:
      1. Customer number (CUST000001)
      2. Party barcode
      3. Party name prefix
      4. Order number (pass to session state for backoffice)
    """
    # Try customer_no match
    if "customer_no" in parties_df.columns:
        match = parties_df[
            parties_df["customer_no"].astype(str).str.upper().str.strip() == code
        ]
        if not match.empty:
            row = match.iloc[0]
            st.session_state["oh_scanned_party"] = row.to_dict()
            st.info(f"🏢 Found by Customer#: **{row['party_name']}** ({row.get('party_type','')})")
            return

    # Try barcode match
    if "barcode" in parties_df.columns:
        match = parties_df[
            parties_df["barcode"].astype(str).str.upper().str.strip() == code
        ]
        if not match.empty:
            row = match.iloc[0]
            st.session_state["oh_scanned_party"] = row.to_dict()
            st.info(f"🏢 Found by barcode: **{row['party_name']}** ({row.get('party_type','')})")
            return

    # Try DB lookup by mobile number (barcode/customer_no not in schema)
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT party_name, party_type, mobile,
                   COALESCE(city,'') AS city
            FROM parties
            WHERE UPPER(TRIM(COALESCE(mobile,'')))     = %s
               OR UPPER(TRIM(COALESCE(alt_mobile,''))) = %s
            LIMIT 1
        """, (code, code)) or []

        if rows:
            st.session_state["oh_scanned_party"] = rows[0]
            st.info(
                f"🏢 Party found: **{rows[0]['party_name']}** "
                f"({rows[0].get('party_type','')}) | "
                f"📞 {rows[0].get('mobile','—')}"
            )
            return
    except Exception:
        pass

    # Try order number
    try:
        from modules.sql_adapter import run_query
        order = run_query(
            "SELECT order_no, status, party_name FROM orders WHERE UPPER(TRIM(order_no))=%s LIMIT 1",
            (code,)
        ) or []
        if order:
            o = order[0]
            st.info(
                f"📋 Order found: **{o['order_no']}** | {o['party_name']} | {o['status']}"
            )
            st.session_state["scanned_order"] = o
            return
    except Exception:
        pass

    st.warning(f"⚠️ **{code}** — not found as party barcode, customer number, or order. Select manually below.")
