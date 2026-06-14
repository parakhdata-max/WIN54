"""
modules/reports/reports_ui.py  
All reports using only tables that exist in the DB.
"""
import streamlit as st
import pandas as pd
from datetime import date, timedelta
from urllib.parse import quote_plus


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        result = run_query(sql, params or {})
        return result or []
    except Exception as ex:
        st.error(f"DB error: {ex}")
        import traceback
        st.code(traceback.format_exc())
        return []


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _shop():
    try:
        from modules.sql_adapter import run_query
        r = run_query("SELECT value FROM system_flags WHERE key='shop_name' LIMIT 1") or []
        return r[0].get("value","DV Optical") if r else "DV Optical"
    except: return "DV Optical"


def _date_filter(key="r"):
    presets = {
        "Today":         (date.today(), date.today()),
        "This week":     (date.today()-timedelta(days=date.today().weekday()), date.today()),
        "This month":    (date.today().replace(day=1), date.today()),
        "Last month":    ((date.today().replace(day=1)-timedelta(days=1)).replace(day=1),
                          date.today().replace(day=1)-timedelta(days=1)),
        "Last 3 months": (date.today()-timedelta(days=90), date.today()),
        "This year":     (date.today().replace(month=1,day=1), date.today()),
        "All time":      (date(2020,1,1), date.today()),
    }
    c1,c2,c3 = st.columns([1,1,2])
    pre_key = f"{key}_pre"
    prev_key = f"{key}_pre_prev"
    preset = c3.selectbox("Period", list(presets.keys()), index=2, key=pre_key)
    df,dt = presets[preset]
    if st.session_state.get(prev_key) != preset:
        st.session_state[f"{key}_fd"] = df
        st.session_state[f"{key}_td"] = dt
        st.session_state[prev_key] = preset
    return c1.date_input("From",value=df,key=f"{key}_fd"), c2.date_input("To",value=dt,key=f"{key}_td")


def _wa_link(mobile: str, message: str) -> str:
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return "https://wa.me/{}?text={}".format(digits, quote_plus(message or ""))


@st.cache_data(ttl=120, show_spinner=False)
def _load_party_names() -> list:
    """Cache party list 2 min — called on every tab, no need to re-query each time."""
    rows = _q("SELECT party_name FROM parties WHERE COALESCE(is_active,true)=true ORDER BY party_name")
    return [r["party_name"] for r in rows]


def _party_picker(key="r"):
    names = ["All Parties"] + _load_party_names()
    return st.selectbox("Party", names, key=f"{key}_pty")


def _party_detail_by_name(party_name: str) -> dict:
    if not party_name or party_name == "All Parties":
        return {}
    rows = _q("""
        SELECT party_name, COALESCE(mobile,'') AS mobile
        FROM parties
        WHERE party_name=%(pn)s
        LIMIT 1
    """, {"pn": party_name})
    return rows[0] if rows else {}


def _qty_display_for_report(row) -> str:
    try:
        qty = float(row.get("Qty") or 0)
    except Exception:
        qty = 0.0
    qty_i = int(round(qty))
    product_text = " ".join(str(row.get(k) or "") for k in ["Category", "Product", "Unit"]).lower()
    try:
        box_size = int(float(row.get("_Box Size") or 1))
    except Exception:
        box_size = 1
    if box_size > 1:
        boxes = qty_i // box_size
        pcs = qty_i % box_size
        if boxes and pcs:
            return f"{boxes} box + {pcs} pcs ({qty_i} pcs)"
        if boxes:
            return f"{boxes} box ({qty_i} pcs)"
        return f"{qty_i} pcs"
    if "ophthalmic" in product_text or ("lens" in product_text and "contact" not in product_text):
        pairs = qty_i // 2
        pcs = qty_i % 2
        if pairs and pcs:
            return f"{pairs} pair + {pcs} pc ({qty_i} pcs)"
        if pairs:
            return f"{pairs} pair ({qty_i} pcs)"
        return f"{qty_i} pc"
    return f"{qty_i} pcs" if abs(qty - qty_i) < 0.0001 else f"{qty:g} pcs"


def _print_btn(df, title, key):
    c1,c2,c3,c4 = st.columns(4)
    with c1:
        st.download_button("⬇ CSV",
            df.to_csv(index=False).encode(),
            file_name=f"{title[:30].replace(' ','_')}.csv",
            mime="text/csv", key=f"{key}_csv", use_container_width=True)
    with c2:
        try:
            from modules.reports.registers import _df_to_excel_bytes
            st.download_button(
                "⬇ Excel",
                _df_to_excel_bytes(df, title),
                file_name=f"{title[:30].replace(' ','_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"{key}_xlsx",
                use_container_width=True,
            )
        except Exception as exc:
            st.button("⬇ Excel", disabled=True, key=f"{key}_xlsx_dis",
                      help=str(exc), use_container_width=True)
    with c3:
        if st.button("🖨 Direct", key=f"{key}_direct", use_container_width=True):
            try:
                from modules.printing.direct_print import spool_html_to_printer
                from modules.printing.print_opener import open_html_print
                from modules.printing.printer_config import load_printer_settings
                html = _do_print(df, title, return_html=True)
                ok, msg = spool_html_to_printer(html, job_name=title[:50])
                if ok:
                    st.success(msg)
                else:
                    st.warning(f"Direct print unavailable: {msg}")
                    if bool(load_printer_settings().get("html_fallback", True)):
                        path = open_html_print(html, f"{title[:30].replace(' ','_')}.html")
                        st.info(f"Opened HTML standby: {path}")
            except Exception as exc:
                st.error(f"Direct print failed: {exc}")
    with c4:
        if st.button("Browser Print", key=f"{key}_prt", use_container_width=True):
            _do_print(df, title)


def _do_print(df, title, return_html=False):
    import streamlit.components.v1 as components
    sh = _shop()
    hdr = "".join(f'<th style="padding:5px 8px;background:#1e3a5f;color:#fff;font-size:11px">{c}</th>' for c in df.columns)
    bdy = ""
    for i,(_,row) in enumerate(df.iterrows()):
        bg = "#f8fafc" if i%2==0 else "#fff"
        cells = "".join(f'<td style="padding:3px 7px;border:0.5px solid #e2e8f0;font-size:10px;background:{bg}">{v}</td>' for v in row)
        bdy += f"<tr>{cells}</tr>"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:Arial,sans-serif;padding:10mm}}
    @media print{{@page{{size:A4 landscape;margin:6mm}}}}</style></head><body>
    <div style="display:flex;justify-content:space-between;margin-bottom:8px">
      <div><div style="font-size:15px;font-weight:900">{sh.upper()}</div>
           <div style="font-size:12px;font-weight:700;color:#1e3a5f">{title}</div></div>
      <div style="font-size:10px;color:#64748b">{date.today().strftime("%d %b %Y")}</div>
    </div>
    <table style="width:100%;border-collapse:collapse">
      <thead><tr>{hdr}</tr></thead><tbody>{bdy}</tbody></table>
    <script>window.onload=function(){{window.print()}}</script></body></html>"""
    if return_html:
        return html
    components.html(html, height=700, scrolling=True)


# ── TAB 1: LEDGER ─────────────────────────────────────────────────────────────
def _tab_ledger():
    try:
        from modules.reports.registers import render_party_ledger
        st.caption("Uses the same settled Party Ledger from Registers: opening balance, covered challans, print, WhatsApp, and email share.")
        render_party_ledger()
        return
    except Exception as exc:
        st.warning(f"Shared Registers ledger could not load, showing legacy Reports ledger. Error: {exc}")

    st.caption("Party account statement — invoices issued vs payments received")
    c1,c2,c3 = st.columns([2,1,1])
    with c1: fd,td = _date_filter("ldg")
    with c2: pty = _party_picker("ldg")
    with c3:
        view = st.selectbox("Ledger View", ["Combined", "Invoices", "Challans"], key="ldg_view")

    party_row = {}
    if pty != "All Parties":
        pr = _q("""
            SELECT id::text, party_name, COALESCE(mobile,'') AS mobile,
                   COALESCE(opening_balance,0) AS opening_balance,
                   UPPER(COALESCE(balance_type,'DR')) AS balance_type
            FROM parties
            WHERE party_name=%(pty)s
            LIMIT 1
        """, {"pty": pty})
        party_row = pr[0] if pr else {}
    pty_filter = "" if pty == "All Parties" else "AND party_name = %(pty)s"

    # DEBUG — check what tables exist
    if st.checkbox("Show debug info", key="ldg_debug"):
        tables = _q("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' ORDER BY table_name
        """)
        st.write("Tables in DB:", [r["table_name"] for r in tables])
        inv_count = _q("SELECT COUNT(*) AS n FROM invoices")
        chal_count = _q("SELECT COUNT(*) AS n FROM challans")
        order_count = _q("SELECT COUNT(*) AS n FROM orders")
        st.write(f"Invoices: {inv_count[0]['n'] if inv_count else 'error'} | "
                 f"Challans: {chal_count[0]['n'] if chal_count else 'error'} | "
                 f"Orders: {order_count[0]['n'] if order_count else 'error'}")

    rows = _q(f"""
        WITH inv AS (
            SELECT i.invoice_date::date AS entry_date,
                   'INVOICE'::text AS entry_type,
                   COALESCE(p.party_name, ox.party_name, ox.patient_name, 'Unknown') AS party_name,
                   i.invoice_no AS ref_no,
                   COALESCE(i.grand_total,0) AS debit,
                   0::numeric AS credit,
                   COALESCE(i.payment_status, i.status, '') AS remarks,
                   FALSE AS covered_challan
            FROM invoices i
            LEFT JOIN parties p ON p.id=i.party_id
            LEFT JOIN LATERAL (
                SELECT o.party_name, o.patient_name
                FROM orders o
                WHERE o.id::text = ANY(COALESCE(i.order_ids::text[], ARRAY[]::text[]))
                LIMIT 1
            ) ox ON TRUE
            WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
              AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
        ),
        ch AS (
            SELECT c.challan_date::date AS entry_date,
                   'CHALLAN'::text AS entry_type,
                   COALESCE(p.party_name, ox.party_name, ox.patient_name, 'Unknown') AS party_name,
                   c.challan_no AS ref_no,
                   COALESCE(c.grand_total,c.total_amount,0) AS debit,
                   0::numeric AS credit,
                   COALESCE(c.status,'') AS remarks,
                   EXISTS (
                       SELECT 1 FROM invoices i2
                       WHERE i2.challan_id = c.id
                         AND COALESCE(i2.is_deleted,FALSE) = FALSE
                   ) AS covered_challan
            FROM challans c
            LEFT JOIN parties p ON p.id=c.party_id
            LEFT JOIN LATERAL (
                SELECT o.party_name, o.patient_name
                FROM orders o
                WHERE o.id::text = ANY(COALESCE(c.order_ids::text[], ARRAY[]::text[]))
                LIMIT 1
            ) ox ON TRUE
            WHERE c.challan_date BETWEEN %(fd)s AND %(td)s
              AND UPPER(COALESCE(c.status,'')) != 'CANCELLED'
        ),
        pay AS (
            SELECT p.payment_date::date AS entry_date,
                   'PAYMENT'::text AS entry_type,
                   COALESCE(pp.party_name, p.party_name, ip.party_name, cp.party_name, op.party_name, 'Unknown') AS party_name,
                   COALESCE(p.payment_no, p.id::text) AS ref_no,
                   0::numeric AS debit,
                   COALESCE(p.amount,0) AS credit,
                   COALESCE(p.remarks, p.payment_mode, p.method, '') AS remarks,
                   FALSE AS covered_challan
            FROM payments p
            LEFT JOIN parties pp ON pp.id=p.party_id
            LEFT JOIN invoices i ON i.id=p.invoice_id
            LEFT JOIN parties ip ON ip.id=i.party_id
            LEFT JOIN challans c ON c.id=p.challan_id
            LEFT JOIN parties cp ON cp.id=c.party_id
            LEFT JOIN orders o ON o.id=COALESCE(p.order_id, p.advance_for_order_id)
            LEFT JOIN parties op ON op.id=o.party_id
            WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
              AND NOT COALESCE(p.is_deleted,FALSE)
              AND COALESCE(NULLIF(p.payment_type,''), 'PAYMENT')
                  IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
        ),
        base AS (
            SELECT * FROM inv
            UNION ALL SELECT * FROM ch
            UNION ALL SELECT * FROM pay
        )
        SELECT entry_date::text AS "Date",
               entry_type AS "Type",
               party_name AS "Party",
               ref_no AS "Document",
               ROUND(debit,2) AS "Dr (\u20b9)",
               ROUND(credit,2) AS "Cr (\u20b9)",
               CASE
                   WHEN covered_challan THEN CONCAT(remarks, ' · covered by invoice')
                   ELSE remarks
               END AS "Remarks",
               covered_challan AS "_covered_challan"
        FROM base
        WHERE 1=1 {pty_filter}
        ORDER BY party_name, entry_date, ref_no
    """, {"fd":fd, "td":td, "pty":pty})

    all_rows = rows or []
    if pty != "All Parties" and party_row:
        opening = float(party_row.get("opening_balance") or 0)
        btype = str(party_row.get("balance_type") or "DR").upper()
        if opening > 0 and btype not in ("CR", "CREDIT"):
            all_rows.insert(0, {
                "Date": str(fd),
                "Type": "OPENING",
                "Party": pty,
                "Document": "Opening Balance",
                "Dr (\u20b9)": opening,
                "Cr (\u20b9)": 0,
                "Remarks": "Opening balance from Party Master",
                "_covered_challan": False,
            })

    if not all_rows:
        st.info("No invoices or challans found for this period.")
        st.caption("💡 Tip: Use **📋 Columnar** tab to see order-level data — it shows all orders even without invoices.")
        return

    df = _df(all_rows)
    if view == "Invoices":
        df = df[df["Type"].isin(["OPENING", "INVOICE", "PAYMENT"])]
    elif view == "Challans":
        df = df[df["Type"].isin(["OPENING", "CHALLAN", "PAYMENT"])]
    else:
        df = df[~((df["Type"] == "CHALLAN") & (df["_covered_challan"] == True))]
    if df.empty:
        st.info(f"No {view.lower()} ledger entries found for this period.")
        return
    df["Dr (\u20b9)"] = pd.to_numeric(df["Dr (\u20b9)"],errors="coerce").fillna(0)
    df["Cr (\u20b9)"] = pd.to_numeric(df["Cr (\u20b9)"],errors="coerce").fillna(0)
    df = df.sort_values(["Party","Date"])
    df["_covered_challan"] = df.get("_covered_challan", False).fillna(False).astype(bool)
    df["_balance_dr"] = df["Dr (\u20b9)"]
    df.loc[df["_covered_challan"], "_balance_dr"] = 0
    df["_net_movement"] = df["_balance_dr"] - df["Cr (\u20b9)"]
    df["Balance (\u20b9)"] = df.groupby("Party", sort=False)["_net_movement"].cumsum()

    m1,m2,m3,m4 = st.columns(4)
    dr=df["Dr (\u20b9)"].sum(); cr=df["Cr (\u20b9)"].sum()
    covered_dr = df.loc[df["_covered_challan"], "Dr (\u20b9)"].sum()
    payable_dr = df["_balance_dr"].sum()
    m1.metric("Total Debit",     f"\u20b9{dr:,.0f}")
    m2.metric("Covered Challans", f"\u20b9{covered_dr:,.0f}")
    m3.metric("Collected",       f"\u20b9{cr:,.0f}")
    m4.metric("Payable Outstanding", f"\u20b9{payable_dr-cr:,.0f}")

    if pty != "All Parties":
        mini = df[df["Type"].eq("INVOICE")].sort_values("Date", ascending=False).head(5)
        lines = [
            f"Hello {pty}",
            f"{view} ledger summary",
            f"Period: {fd} to {td}",
            f"Opening: \u20b9{float(party_row.get('opening_balance') or 0):,.2f}",
            f"Payable outstanding: \u20b9{payable_dr-cr:,.2f}",
            f"Covered challans excluded: \u20b9{covered_dr:,.2f}",
            "",
            "Last 5 invoices:",
        ]
        if mini.empty:
            lines.append("No invoices in selected view/period.")
        else:
            for _, r in mini.iterrows():
                lines.append(f"{r['Date']} · {r['Document']} · \u20b9{float(r['Dr (\u20b9)']):,.2f}")
        lines.append("")
        lines.append("Thank you.")
        msg_key = "ldg_wa_msg"
        msg_src = "ldg_wa_src"
        src = f"{pty}|{fd}|{td}|{view}|{payable_dr}|{covered_dr}|{cr}"
        if st.session_state.get(msg_src) != src:
            st.session_state[msg_key] = "\n".join(lines)
            st.session_state[msg_src] = src
        with st.expander("WhatsApp mini ledger", expanded=False):
            try:
                from modules.wa_contact_tools import render_mobile_field
                mobile = render_mobile_field(
                    "ldg_wa",
                    name=pty,
                    mobile=str(party_row.get("mobile") or ""),
                    label="Mobile",
                )
            except Exception:
                mobile = st.text_input("Mobile", value=str(party_row.get("mobile") or ""), key="ldg_wa_mobile")
            edited = st.text_area("Message", key=msg_key, height=190)
            st.link_button("Send WhatsApp Ledger", _wa_link(mobile, edited), use_container_width=True)
            st.download_button(
                "Download Ledger CSV",
                df.drop(columns=["_covered_challan", "_balance_dr", "_net_movement"], errors="ignore").to_csv(index=False).encode(),
                file_name=f"ledger_{pty}_{fd}_{td}.csv".replace(" ", "_"),
                mime="text/csv",
                key="ldg_party_csv",
                use_container_width=True,
            )

    show_df = df.drop(columns=["_covered_challan", "_balance_dr", "_net_movement"], errors="ignore")
    st.dataframe(show_df, use_container_width=True, hide_index=True,
        column_config={
            "Dr (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Cr (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Balance (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
        })
    _print_btn(show_df, f"{view} Ledger {pty} {fd} to {td}", "ldg")


# ── TAB 2: PRODUCT SALES ──────────────────────────────────────────────────────
def _tab_product_sales():
    st.caption("Product-wise and party-wise sales from order lines")
    fd,td = _date_filter("pws")
    f1, f2, f3 = st.columns([1.3, 1.3, 1])
    with f1:
        pty = _party_picker("pws")
    with f2:
        report_view = st.selectbox(
            "Report",
            ["Product Summary", "Discount Detail"],
            key="pws_view",
        )
    @st.cache_data(ttl=300, show_spinner=False)
    def _get_main_groups():
        return [r["main_group"] for r in
                _q("SELECT DISTINCT main_group FROM products WHERE main_group IS NOT NULL ORDER BY main_group")]
    mgs = ["All Groups"] + _get_main_groups()
    with f3:
        mg = st.selectbox("Category", mgs, key="pws_mg")
    gf = "" if mg=="All Groups" else "AND p.main_group=%(mg)s"
    pf = "" if pty=="All Parties" else "AND COALESCE(pa.party_name,o.party_name,'')=%(pty)s"

    if report_view == "Product Summary":
        rows = _q(f"""
            SELECT
                COALESCE(pa.party_name,o.party_name,'—') AS "Party",
                COALESCE(p.main_group,'—') AS "Category",
                p.product_name              AS "Product",
                COALESCE(p.brand,'—')     AS "Brand",
                COALESCE(p.unit,'PCS')     AS "Unit",
                COALESCE(p.box_size,1)     AS "_Box Size",
                SUM(ol.quantity)            AS "Qty",
                ROUND(SUM(COALESCE(ol.unit_price,0) * COALESCE(ol.quantity,0)),2) AS "Gross (\u20b9)",
                ROUND(SUM(COALESCE(ol.discount_amount,0)),2) AS "Discount (\u20b9)",
                ROUND(SUM(GREATEST(
                    COALESCE(ol.billing_total, ol.total_price,
                        COALESCE(ol.unit_price,0) * COALESCE(ol.quantity,0)
                        - COALESCE(ol.discount_amount,0)
                    ), 0
                )),2) AS "Net (\u20b9)",
                COUNT(DISTINCT o.id) AS "Orders"
            FROM order_lines ol
            JOIN orders o   ON o.id=ol.order_id
            LEFT JOIN parties pa ON pa.id=o.party_id
            JOIN products p ON p.id=ol.product_id
            WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(o.is_deleted,false)=false
              AND COALESCE(ol.is_deleted,false)=false
              {gf} {pf}
            GROUP BY COALESCE(pa.party_name,o.party_name,'—'), p.main_group, p.product_name, p.brand, p.unit, p.box_size
            ORDER BY "Net (\u20b9)" DESC NULLS LAST
        """, {"fd":fd,"td":td,"mg":mg,"pty":pty})
    else:
        rows = _q(f"""
            SELECT
                o.created_at::date::text AS "Date",
                o.order_no AS "Order",
                COALESCE(pa.party_name,o.party_name,'—') AS "Party",
                COALESCE(p.main_group,'—') AS "Category",
                COALESCE(p.product_name,'—') AS "Product",
                COALESCE(p.brand,'—') AS "Brand",
                COALESCE(p.unit,'PCS') AS "Unit",
                COALESCE(p.box_size,1) AS "_Box Size",
                COALESCE(ol.eye_side,'') AS "Eye",
                COALESCE(ol.quantity,0) AS "Qty",
                COALESCE(ol.unit_price,0) AS "Rate (\u20b9)",
                ROUND(COALESCE(ol.unit_price,0) * COALESCE(ol.quantity,0),2) AS "Gross (\u20b9)",
                COALESCE(ol.discount_percent,0) AS "Discount %%",
                COALESCE(ol.discount_amount,0) AS "Discount (\u20b9)",
                COALESCE(ol.discount_rule,'') AS "Discount Rule",
                ROUND(GREATEST(
                    COALESCE(ol.billing_total, ol.total_price,
                        COALESCE(ol.unit_price,0) * COALESCE(ol.quantity,0)
                        - COALESCE(ol.discount_amount,0)
                    ), 0
                ),2) AS "Net (\u20b9)",
                o.status AS "Status"
            FROM order_lines ol
            JOIN orders o   ON o.id=ol.order_id
            LEFT JOIN parties pa ON pa.id=o.party_id
            LEFT JOIN products p ON p.id=ol.product_id
            WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(o.is_deleted,false)=false
              AND COALESCE(ol.is_deleted,false)=false
              {gf} {pf}
            ORDER BY o.created_at DESC, o.order_no, p.product_name
        """, {"fd":fd,"td":td,"mg":mg,"pty":pty})

    if not rows:
        check = _q("SELECT COUNT(*) AS n FROM orders WHERE COALESCE(is_deleted,false)=false")
        n = int(check[0].get("n",0)) if check else 0
        if n > 0:
            st.warning(f"No order lines found in selected period. {n} orders exist — try **All time**.")
        else:
            st.info("No orders found in DB yet.")
        return

    df = _df(rows)
    for c in ["Qty","Gross (\u20b9)","Discount %","Discount (\u20b9)","Net (\u20b9)","Orders","Rate (\u20b9)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c],errors="coerce").fillna(0)
    if "Qty" in df.columns:
        df["Qty Display"] = df.apply(_qty_display_for_report, axis=1)
        cols = list(df.columns)
        if "Qty Display" in cols and "Qty" in cols:
            cols.insert(cols.index("Qty") + 1, cols.pop(cols.index("Qty Display")))
            df = df[cols]

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Gross",        f"\u20b9{df.get('Gross (\u20b9)', pd.Series([0])).sum():,.0f}")
    m2.metric("Discount",     f"\u20b9{df.get('Discount (\u20b9)', pd.Series([0])).sum():,.0f}")
    m3.metric("Units",        f"{int(df['Qty'].sum()):,}")
    m4.metric("Net",          f"\u20b9{df.get('Net (\u20b9)', pd.Series([0])).sum():,.0f}")

    if pty != "All Parties":
        party_info = _party_detail_by_name(pty)
        send_df = df.drop(columns=["_Box Size"], errors="ignore")
        top = send_df.sort_values("Net (\u20b9)", ascending=False).head(8)
        lines = [
            f"Hello {pty}",
            f"{report_view} report",
            f"Period: {fd} to {td}",
            f"Gross: \u20b9{df.get('Gross (\u20b9)', pd.Series([0])).sum():,.2f}",
            f"Discount: \u20b9{df.get('Discount (\u20b9)', pd.Series([0])).sum():,.2f}",
            f"Net: \u20b9{df.get('Net (\u20b9)', pd.Series([0])).sum():,.2f}",
            "",
            "Top lines:",
        ]
        for _, r in top.iterrows():
            label = r.get("Product") or r.get("Order") or ""
            lines.append(f"{label} · {r.get('Qty Display') or ('Qty ' + str(r.get('Qty') or ''))} · \u20b9{float(r.get('Net (\u20b9)') or 0):,.2f}")
        lines.append("")
        lines.append("Detailed CSV/download is available from report.")
        src = f"{pty}|{fd}|{td}|{mg}|{report_view}|{len(df)}|{df.get('Net (\u20b9)', pd.Series([0])).sum()}"
        msg_key = "pws_wa_msg"
        msg_src = "pws_wa_src"
        if st.session_state.get(msg_src) != src:
            st.session_state[msg_key] = "\n".join(lines)
            st.session_state[msg_src] = src
        with st.expander("WhatsApp / download for party", expanded=False):
            try:
                from modules.wa_contact_tools import render_mobile_field
                mobile = render_mobile_field(
                    "pws_wa",
                    name=pty,
                    mobile=str(party_info.get("mobile") or ""),
                    label="Mobile",
                )
            except Exception:
                mobile = st.text_input("Mobile", value=str(party_info.get("mobile") or ""), key="pws_wa_mobile")
            edited = st.text_area("Message", key=msg_key, height=190)
            st.link_button("Send WhatsApp Report", _wa_link(mobile, edited), use_container_width=True)
            st.download_button(
                "Download Detailed CSV",
                send_df.to_csv(index=False).encode(),
                file_name=f"{report_view}_{pty}_{fd}_{td}.csv".replace(" ", "_"),
                mime="text/csv",
                key="pws_party_csv",
                use_container_width=True,
            )

    show_df = df.drop(columns=["_Box Size"], errors="ignore")
    st.dataframe(show_df, use_container_width=True, hide_index=True)
    _print_btn(show_df, f"{report_view} {pty} {fd} to {td}", "pws")


# ── TAB 3: COLUMNAR ───────────────────────────────────────────────────────────
def _tab_columnar():
    st.caption("Order-wise detail — party, patient, product, Rx, price, status")
    fd,td = _date_filter("col")
    c1,c2 = st.columns(2)
    with c1: pty = _party_picker("col")
    with c2:
        sts = ["All","PENDING","CONFIRMED","IN_PRODUCTION","READY","BILLED","DISPATCHED","DELIVERED","CLOSED"]
        st_sel = st.selectbox("Status", sts, key="col_st")
    pf = "" if pty=="All Parties" else "AND COALESCE(pa.party_name,o.party_name,'')=%(pty)s"
    sf = "" if st_sel=="All" else "AND o.status=%(st)s"

    # Count first for pagination
    _cnt = _q(f"""
        SELECT COUNT(*) AS n FROM orders o
        LEFT JOIN parties pa ON pa.id=o.party_id
        JOIN order_lines ol  ON ol.order_id=o.id
        WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(o.is_deleted,false)=false
          AND COALESCE(ol.is_deleted,false)=false
          {pf} {sf}
    """, {"fd":fd,"td":td,"pty":pty,"st":st_sel})
    _total = int((_cnt[0]["n"] if _cnt else 0) or 0)
    PAGE   = 200
    _pages = max(1, (_total + PAGE - 1) // PAGE)
    _page  = st.number_input(f"Page (1–{_pages})", min_value=1,
                              max_value=_pages, value=1,
                              step=1, key="col_page") if _pages > 1 else 1
    _off   = (_page - 1) * PAGE

    rows = _q(f"""
        SELECT
            o.order_no                          AS "Order",
            o.created_at::date::text            AS "Date",
            COALESCE(pa.party_name,o.party_name,'—') AS "Party",
            COALESCE(o.patient_name,'—')      AS "Patient",
            COALESCE(pr.main_group,'—')       AS "Category",
            COALESCE(pr.product_name,'—')     AS "Product",
            COALESCE(pr.brand,'')             AS "Brand",
            COALESCE(pr.unit,'PCS')            AS "Unit",
            COALESCE(pr.box_size,1)            AS "_Box Size",
            COALESCE(ol.eye_side,'')          AS "Eye",
            COALESCE(ol.sph::text,'')         AS "SPH",
            COALESCE(ol.cyl::text,'')         AS "CYL",
            COALESCE(ol.axis::text,'')        AS "AXIS",
            COALESCE(ol.add_power::text,'')   AS "ADD",
            ol.quantity                         AS "Qty",
            ol.unit_price                       AS "Rate (\u20b9)",
            ROUND(COALESCE(ol.unit_price,0)*COALESCE(ol.quantity,0),2) AS "Gross (\u20b9)",
            COALESCE(ol.discount_percent,0)     AS "Discount %%",
            COALESCE(ol.discount_amount,0)      AS "Discount (\u20b9)",
            COALESCE(ol.discount_rule,'')       AS "Discount Rule",
            ROUND(GREATEST(
                COALESCE(ol.billing_total, ol.total_price,
                    COALESCE(ol.unit_price,0)*COALESCE(ol.quantity,0)
                    - COALESCE(ol.discount_amount,0)
                ), 0
            ),2)                                AS "Net (\u20b9)",
            o.status                            AS "Status"
        FROM orders o
        LEFT JOIN parties pa ON pa.id=o.party_id
        JOIN order_lines ol  ON ol.order_id=o.id
        JOIN products pr     ON pr.id=ol.product_id
        WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(o.is_deleted,false)=false
          AND COALESCE(ol.is_deleted,false)=false
          {pf} {sf}
        ORDER BY o.created_at DESC
        LIMIT %(lim)s OFFSET %(off)s
    """, {"fd":fd,"td":td,"pty":pty,"st":st_sel,"lim":PAGE,"off":_off})

    if not rows:
        # Try without date filter to check if any orders exist at all
        check = _q("SELECT COUNT(*) AS n, MIN(created_at::date)::text AS earliest, MAX(created_at::date)::text AS latest FROM orders WHERE COALESCE(is_deleted,false)=false")
        if check and int(check[0].get("n",0)) > 0:
            st.warning(
                f"No orders in selected date range ({fd} to {td}). "
                f"Orders exist from **{check[0].get('earliest')}** to **{check[0].get('latest')}** "
                f"({check[0].get('n')} total). Try **All time** in Period selector."
            )
        else:
            st.info("No orders found in DB yet.")
        return

    df = _df(rows)
    if "Qty" in df.columns:
        df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0)
        df["Qty Display"] = df.apply(_qty_display_for_report, axis=1)
        cols = list(df.columns)
        cols.insert(cols.index("Qty") + 1, cols.pop(cols.index("Qty Display")))
        df = df[cols]
    val = pd.to_numeric(df.get("Net (\u20b9)",pd.Series([0])),errors="coerce").sum()
    st.caption(f"Page {_page}/{_pages} · {len(df)} of {_total:,} lines · \u20b9{val:,.0f} this page")
    show_df = df.drop(columns=["_Box Size"], errors="ignore")
    st.dataframe(show_df, use_container_width=True, hide_index=True,
        column_config={
            "Rate (\u20b9)":  st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Gross (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Discount (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Net (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
        })
    _print_btn(show_df, f"Columnar {fd} to {td}", "col")


# ── TAB 4: CREDIT DAYS ────────────────────────────────────────────────────────
def _tab_credit_days():
    st.caption("Outstanding invoices — aging by party")
    c1,c2 = st.columns(2)
    with c1: overdue = st.checkbox("Overdue only", value=True, key="crd_ov")
    with c2: pty = _party_picker("crd")
    of = "AND i.due_date < CURRENT_DATE" if overdue else ""
    pf = "" if pty=="All Parties" else "AND p.party_name=%(pty)s"

    rows = _q(f"""
        SELECT
            p.party_name                        AS "Party",
            COALESCE(p.mobile,'—')            AS "Mobile",
            i.invoice_no                        AS "Invoice",
            i.invoice_date::text                AS "Inv Date",
            COALESCE(i.due_date::text,'—')    AS "Due Date",
            CASE WHEN i.due_date IS NOT NULL
                 THEN (CURRENT_DATE-i.due_date)
                 ELSE 0 END                     AS "Days Over",
            i.grand_total                       AS "Amount (\u20b9)",
            i.payment_status                    AS "Payment",
            CASE
              WHEN i.due_date IS NULL OR i.due_date>=CURRENT_DATE THEN 'Current'
              WHEN (CURRENT_DATE-i.due_date)<=30  THEN '0-30 days'
              WHEN (CURRENT_DATE-i.due_date)<=60  THEN '31-60 days'
              WHEN (CURRENT_DATE-i.due_date)<=90  THEN '61-90 days'
              ELSE '90+ days'
            END                                 AS "Bucket"
        FROM invoices i
        JOIN parties p ON p.id=i.party_id
        WHERE i.payment_status IN ('UNPAID','PARTIAL')
          AND i.status NOT IN ('CANCELLED','VOID')
          {of} {pf}
        ORDER BY (CURRENT_DATE-COALESCE(i.due_date,CURRENT_DATE)) DESC
    """, {"pty":pty})

    if not rows:
        st.success("\u2705 No outstanding invoices found!")
        return

    df = _df(rows)
    df["Amount (\u20b9)"] = pd.to_numeric(df["Amount (\u20b9)"],errors="coerce").fillna(0)

    bucket_order = ["Current","0-30 days","31-60 days","61-90 days","90+ days"]
    buckets = df.groupby("Bucket")["Amount (\u20b9)"].sum().reindex(bucket_order, fill_value=0)
    cols = st.columns(5)
    for i,(bk,amt) in enumerate(buckets.items()):
        cols[i].metric(bk, f"\u20b9{amt:,.0f}")

    st.markdown("---")
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={"Amount (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f")})
    _print_btn(df, "Credit Days Report", "crd")


# ── TAB 5: CHALLAN REGISTER ───────────────────────────────────────────────────
def _tab_challan_register():
    st.caption("Challan register — all dispatches with value and status")
    fd,td = _date_filter("chr")
    pty = _party_picker("chr")
    pf = "" if pty=="All Parties" else "AND p.party_name=%(pty)s"

    rows = _q(f"""
        SELECT
            c.challan_no                        AS "Challan No",
            c.challan_date::text                AS "Date",
            p.party_name                        AS "Party",
            c.total_amount                      AS "Amount (\u20b9)",
            c.total_tax                         AS "Tax (\u20b9)",
            c.grand_total                       AS "Total (\u20b9)",
            c.status                            AS "Status",
            COALESCE(c.remarks,'')            AS "Remarks"
        FROM challans c
        JOIN parties p ON p.id=c.party_id
        WHERE c.challan_date BETWEEN %(fd)s AND %(td)s
          AND c.status!='CANCELLED' {pf}
        ORDER BY c.challan_date DESC
    """, {"fd":fd,"td":td,"pty":pty})

    if not rows:
        st.info("No challans found.")
        return

    df = _df(rows)
    for c in ["Amount (\u20b9)","Tax (\u20b9)","Total (\u20b9)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c],errors="coerce").fillna(0)

    m1,m2,m3 = st.columns(3)
    m1.metric("Challans",      str(len(df)))
    m2.metric("Total Value",   f"\u20b9{df['Total (\u20b9)'].sum():,.0f}")
    m3.metric("Total Tax",     f"\u20b9{df['Tax (\u20b9)'].sum():,.0f}")

    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={"Total (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f")})
    _print_btn(df, f"Challan Register {fd} to {td}", "chr")


# ── TAB 6: STOCK VALUE ────────────────────────────────────────────────────────
def _tab_stock_value():
    st.caption("Current inventory at cost price and MRP")
    mgs = ["All Groups"] + [r["main_group"] for r in
        _q("SELECT DISTINCT main_group FROM products WHERE main_group IS NOT NULL ORDER BY main_group")]
    mg = st.selectbox("Category", mgs, key="stv_mg")
    gf = "" if mg=="All Groups" else "AND p.main_group=%(mg)s"
    show_zero = st.checkbox("Include zero stock", value=False, key="stv_zero")
    zf = "" if show_zero else "AND COALESCE(s.quantity,0)>0"

    rows = _q(f"""
        SELECT
            COALESCE(p.main_group,'—')        AS "Category",
            p.product_name                      AS "Product",
            COALESCE(p.brand,'—')             AS "Brand",
            COALESCE(s.batch_no,'—')          AS "SKU/Batch",
            COALESCE(s.location,'—')          AS "Location",
            COALESCE(s.quantity,0)              AS "Qty",
            COALESCE(s.purchase_rate,0)         AS "Cost (\u20b9)",
            COALESCE(s.mrp,0)                   AS "MRP (\u20b9)",
            COALESCE(s.quantity,0)*COALESCE(s.purchase_rate,0) AS "Cost Value (\u20b9)",
            COALESCE(s.quantity,0)*COALESCE(s.mrp,0)           AS "MRP Value (\u20b9)"
        FROM products p
        JOIN inventory_stock s ON s.product_id=p.id
        WHERE COALESCE(s.is_active,true)=true {gf} {zf}
        ORDER BY p.main_group, p.product_name
    """, {"mg":mg})

    if not rows:
        st.info("No stock found.")
        return

    df = _df(rows)
    for c in ["Qty","Cost (\u20b9)","MRP (\u20b9)","Cost Value (\u20b9)","MRP Value (\u20b9)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c],errors="coerce").fillna(0)

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("SKUs",         str(len(df)))
    m2.metric("Total Qty",    f"{int(df['Qty'].sum()):,}")
    m3.metric("Cost Value",   f"\u20b9{df['Cost Value (\u20b9)'].sum():,.0f}")
    m4.metric("MRP Value",    f"\u20b9{df['MRP Value (\u20b9)'].sum():,.0f}")

    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={
            "Cost (\u20b9)":       st.column_config.NumberColumn(format="\u20b9%.0f"),
            "MRP (\u20b9)":        st.column_config.NumberColumn(format="\u20b9%.0f"),
            "Cost Value (\u20b9)": st.column_config.NumberColumn(format="\u20b9%.0f"),
            "MRP Value (\u20b9)":  st.column_config.NumberColumn(format="\u20b9%.0f"),
        })
    _print_btn(df, f"Stock Valuation {mg}", "stv")



# ── TAB 7: PARTY OUTSTANDING ──────────────────────────────────────────────────
def _tab_outstanding():
    st.caption("Per-party balance: total invoiced minus total collected (relational from payments FK)")

    c1, c2 = st.columns([2, 1])
    with c1: pty = _party_picker("ost")
    with c2: show_zero = st.checkbox("Include settled parties", value=False, key="ost_zero")

    pf = "" if pty == "All Parties" else "AND pl.party_name = %(pty)s"
    zf = "" if show_zero else "HAVING ROUND(SUM(pl.debit) - SUM(pl.credit), 2) > 0.50"

    rows = _q(f"""
        SELECT
            COALESCE(pl.party_name, '—')        AS "Party",
            COUNT(DISTINCT pl.ref_no)           AS "Transactions",
            ROUND(SUM(pl.debit), 2)             AS "Total Invoiced (₹)",
            ROUND(SUM(pl.credit), 2)            AS "Total Paid (₹)",
            ROUND(SUM(pl.debit) - SUM(pl.credit), 2) AS "Outstanding (₹)",
            MAX(pl.entry_date)::text            AS "Last Activity"
        FROM party_ledger pl
        WHERE 1=1 {pf}
        GROUP BY pl.party_name
        {zf}
        ORDER BY ROUND(SUM(pl.debit) - SUM(pl.credit), 2) DESC
    """, {"pty": pty})

    if not rows:
        st.success("✅ No outstanding balances found!")
        return

    df = _df(rows)
    for c in ["Total Invoiced (₹)", "Total Paid (₹)", "Outstanding (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Parties",          str(len(df)))
    m2.metric("Total Invoiced",   f"₹{df['Total Invoiced (₹)'].sum():,.0f}")
    m3.metric("Total Collected",  f"₹{df['Total Paid (₹)'].sum():,.0f}")
    m4.metric("Net Outstanding",  f"₹{df['Outstanding (₹)'].sum():,.0f}",
              delta=f"₹{df['Outstanding (₹)'].sum():,.0f} due",
              delta_color="inverse")

    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={
            "Total Invoiced (₹)":  st.column_config.NumberColumn(format="₹%.2f"),
            "Total Paid (₹)":      st.column_config.NumberColumn(format="₹%.2f"),
            "Outstanding (₹)":     st.column_config.NumberColumn(format="₹%.2f"),
        })

    # Drill-down: select party → show ledger
    if pty != "All Parties":
        with st.expander(f"📒 {pty} — Full Ledger", expanded=False):
            detail = _q("""
                SELECT entry_date::text AS "Date", entry_type AS "Type",
                       ref_no AS "Ref", narration AS "Narration",
                       ROUND(debit, 2) AS "Debit (₹)",
                       ROUND(credit, 2) AS "Credit (₹)"
                FROM party_ledger
                WHERE party_name = %(pty)s
                ORDER BY entry_date ASC, id ASC
            """, {"pty": pty})
            if detail:
                ddf = _df(detail)
                # Running balance
                ddf["Debit (₹)"]  = pd.to_numeric(ddf["Debit (₹)"],  errors="coerce").fillna(0)
                ddf["Credit (₹)"] = pd.to_numeric(ddf["Credit (₹)"], errors="coerce").fillna(0)
                ddf["Balance (₹)"] = (ddf["Debit (₹)"] - ddf["Credit (₹)"]).cumsum().round(2)
                st.dataframe(ddf, use_container_width=True, hide_index=True,
                    column_config={
                        "Debit (₹)":   st.column_config.NumberColumn(format="₹%.2f"),
                        "Credit (₹)":  st.column_config.NumberColumn(format="₹%.2f"),
                        "Balance (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                    })
                _print_btn(ddf, f"Ledger {pty}", "ost_det")

    _print_btn(df, "Party Outstanding Report", "ost")


# ── TAB 8: AGING REPORT ───────────────────────────────────────────────────────
def _tab_aging():
    st.caption("Invoice aging — balance derived from payments.invoice_id FK, not stored payment_status")

    pty = _party_picker("agn")
    pf  = "" if pty == "All Parties" else "AND p.party_name = %(pty)s"

    rows = _q(f"""
        SELECT
            p.party_name                        AS "Party",
            COALESCE(p.mobile, '—')           AS "Mobile",
            i.invoice_no                        AS "Invoice",
            i.invoice_date::text                AS "Invoice Date",
            COALESCE(i.due_date::text, '—')   AS "Due Date",
            ROUND(COALESCE(i.grand_total, 0), 2) AS "Invoice Amt (₹)",
            -- Relational: sum all linked payments
            ROUND(COALESCE((
                SELECT SUM(pm.amount) FROM payments pm
                WHERE pm.invoice_id = i.id
                  AND NOT COALESCE(pm.is_deleted, FALSE)
            ), 0), 2) AS "Paid (₹)",
            -- Balance derived from FK
            ROUND(GREATEST(COALESCE(i.grand_total, 0) - COALESCE((
                SELECT SUM(pm.amount) FROM payments pm
                WHERE pm.invoice_id = i.id
                  AND NOT COALESCE(pm.is_deleted, FALSE)
            ), 0), 0), 2) AS "Balance (₹)",
            -- Days overdue (positive = overdue)
            CASE WHEN i.due_date IS NOT NULL
                 THEN (CURRENT_DATE - i.due_date)
                 ELSE NULL END                  AS "Days Over",
            -- Aging bucket
            CASE
                WHEN i.due_date IS NULL OR i.due_date >= CURRENT_DATE
                     THEN 'Current'
                WHEN (CURRENT_DATE - i.due_date) <= 30  THEN '1-30 days'
                WHEN (CURRENT_DATE - i.due_date) <= 60  THEN '31-60 days'
                WHEN (CURRENT_DATE - i.due_date) <= 90  THEN '61-90 days'
                ELSE '90+ days'
            END                                 AS "Bucket"
        FROM invoices i
        JOIN parties p ON p.id = i.party_id
        WHERE
            -- Only open invoices (balance > 0)
            GREATEST(COALESCE(i.grand_total, 0) - COALESCE((
                SELECT SUM(pm.amount) FROM payments pm
                WHERE pm.invoice_id = i.id
                  AND NOT COALESCE(pm.is_deleted, FALSE)
            ), 0), 0) > 0.50
            AND COALESCE(i.is_deleted, FALSE) = FALSE
            AND UPPER(COALESCE(i.status, 'PENDING')) != 'CANCELLED'
            {pf}
        ORDER BY "Days Over" DESC NULLS LAST, p.party_name
    """, {"pty": pty})

    if not rows:
        st.success("✅ All invoices settled — no outstanding aging!")
        return

    df = _df(rows)
    for c in ["Invoice Amt (₹)", "Paid (₹)", "Balance (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Bucket summary
    bucket_order = ["Current", "1-30 days", "31-60 days", "61-90 days", "90+ days"]
    bucket_totals = df.groupby("Bucket")["Balance (₹)"].sum().reindex(bucket_order, fill_value=0)
    cols = st.columns(5)
    colors = ["#10b981", "#f59e0b", "#f97316", "#ef4444", "#7f1d1d"]
    for i, (bk, amt) in enumerate(bucket_totals.items()):
        cols[i].metric(bk, f"₹{amt:,.0f}")

    total_outstanding = df["Balance (₹)"].sum()
    st.markdown(
        f"<div style='background:#1a0a0a;border:1px solid #ef4444;border-radius:8px;"
        f"padding:10px 16px;margin:8px 0;display:flex;justify-content:space-between'>"
        f"<span style='color:#fca5a5'>{len(df)} open invoices</span>"
        f"<span style='color:#ef4444;font-weight:700;font-size:1.1rem'>"
        f"Total Due: ₹{total_outstanding:,.2f}</span></div>",
        unsafe_allow_html=True,
    )

    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={
            "Invoice Amt (₹)": st.column_config.NumberColumn(format="₹%.2f"),
            "Paid (₹)":        st.column_config.NumberColumn(format="₹%.2f"),
            "Balance (₹)":     st.column_config.NumberColumn(format="₹%.2f"),
        })
    _print_btn(df, "Aging Report", "agn")


# ── TAB 9: DAILY CASH FLOW ────────────────────────────────────────────────────
def _tab_cashflow():
    st.caption("Daily collections by payment mode — cash inflow from payments table")

    fd, td = _date_filter("cf")

    # Daily totals by mode
    rows = _q("""
        SELECT
            payment_date::text  AS "Date",
            payment_mode        AS "Mode",
            COUNT(*)            AS "Count",
            ROUND(SUM(amount), 2) AS "Amount (₹)"
        FROM payments
        WHERE payment_date BETWEEN %(fd)s AND %(td)s
          AND payment_type = 'PAYMENT'
          AND NOT COALESCE(is_deleted, FALSE)
        GROUP BY payment_date, payment_mode
        ORDER BY payment_date DESC, payment_mode
    """, {"fd": fd, "td": td})

    # Daily summary
    daily = _q("""
        SELECT
            payment_date::text      AS "Date",
            COUNT(*)                AS "Receipts",
            ROUND(SUM(amount), 2)  AS "Total (₹)",
            ROUND(SUM(CASE WHEN payment_mode='CASH'  THEN amount ELSE 0 END), 2) AS "Cash (₹)",
            ROUND(SUM(CASE WHEN payment_mode='UPI'   THEN amount ELSE 0 END), 2) AS "UPI (₹)",
            ROUND(SUM(CASE WHEN payment_mode='NEFT'  THEN amount ELSE 0 END), 2) AS "NEFT (₹)",
            ROUND(SUM(CASE WHEN payment_mode='CHEQUE'THEN amount ELSE 0 END), 2) AS "Cheque (₹)",
            ROUND(SUM(CASE WHEN payment_mode NOT IN ('CASH','UPI','NEFT','CHEQUE')
                      THEN amount ELSE 0 END), 2) AS "Other (₹)"
        FROM payments
        WHERE payment_date BETWEEN %(fd)s AND %(td)s
          AND payment_type = 'PAYMENT'
          AND NOT COALESCE(is_deleted, FALSE)
        GROUP BY payment_date
        ORDER BY payment_date DESC
    """, {"fd": fd, "td": td})

    if not rows:
        st.info("No payments found in this period.")
        return

    df_mode = _df(rows)
    df_daily = _df(daily)

    for c in ["Amount (₹)", "Total (₹)", "Cash (₹)", "UPI (₹)", "NEFT (₹)", "Cheque (₹)", "Other (₹)"]:
        for df in [df_mode, df_daily]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    total = df_daily["Total (₹)"].sum() if "Total (₹)" in df_daily.columns else 0
    cash  = df_daily["Cash (₹)"].sum()  if "Cash (₹)"  in df_daily.columns else 0
    upi   = df_daily["UPI (₹)"].sum()   if "UPI (₹)"   in df_daily.columns else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Collected",  f"₹{total:,.0f}")
    m2.metric("Cash",             f"₹{cash:,.0f}")
    m3.metric("UPI / Digital",    f"₹{upi:,.0f}")
    m4.metric("Days",             str(len(df_daily)))

    # Mode breakdown pie (text table)
    mode_summary = df_mode.groupby("Mode")["Amount (₹)"].sum().sort_values(ascending=False)
    st.markdown("**Collection by Mode:**")
    mode_cols = st.columns(len(mode_summary))
    for i, (mode, amt) in enumerate(mode_summary.items()):
        pct = (amt / total * 100) if total > 0 else 0
        mode_cols[i].metric(mode, f"₹{amt:,.0f}", delta=f"{pct:.1f}%", delta_color="off")

    st.markdown("---")
    st.markdown("**Day-wise Breakdown:**")
    st.dataframe(df_daily, use_container_width=True, hide_index=True,
        column_config={
            "Total (₹)":  st.column_config.NumberColumn(format="₹%.0f"),
            "Cash (₹)":   st.column_config.NumberColumn(format="₹%.0f"),
            "UPI (₹)":    st.column_config.NumberColumn(format="₹%.0f"),
            "NEFT (₹)":   st.column_config.NumberColumn(format="₹%.0f"),
            "Cheque (₹)": st.column_config.NumberColumn(format="₹%.0f"),
            "Other (₹)":  st.column_config.NumberColumn(format="₹%.0f"),
        })
    _print_btn(df_daily, f"Cash Flow {fd} to {td}", "cf")


# ── TAB 10: GST SUMMARY ───────────────────────────────────────────────────────
def _tab_gst():
    st.caption("GSTR-1 ready summary — B2B (with GSTIN) and B2C, HSN-wise tax breakup")

    fd, td = _date_filter("gst")
    c1, c2 = st.columns(2)
    with c1:
        gst_view = st.radio("View", ["Summary", "B2B Detail", "B2C Detail", "HSN-wise"],
                            horizontal=True, key="gst_view")

    if gst_view == "Summary":
        rows = _q("""
            SELECT
                CASE WHEN COALESCE(p.gstin,'') != '' THEN 'B2B' ELSE 'B2C' END AS "Type",
                COUNT(DISTINCT i.id)            AS "Invoices",
                ROUND(SUM(i.total_amount), 2)  AS "Taxable Value (₹)",
                ROUND(SUM(i.total_tax), 2)     AS "Total Tax (₹)",
                ROUND(SUM(i.grand_total), 2)   AS "Invoice Value (₹)"
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(i.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(i.status, '')) != 'CANCELLED'
            GROUP BY 1
            ORDER BY 1
        """, {"fd": fd, "td": td})

        if not rows:
            st.info("No invoices found in this period.")
            return

        df = _df(rows)
        for c in ["Taxable Value (₹)", "Total Tax (₹)", "Invoice Value (₹)"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Invoices",    str(df["Invoices"].sum()))
        m2.metric("Taxable Value",     f"₹{df['Taxable Value (₹)'].sum():,.0f}")
        m3.metric("Total GST",         f"₹{df['Total Tax (₹)'].sum():,.0f}")
        m4.metric("Invoice Value",     f"₹{df['Invoice Value (₹)'].sum():,.0f}")
        st.dataframe(df, use_container_width=True, hide_index=True)

    elif gst_view == "B2B Detail":
        rows = _q("""
            SELECT
                i.invoice_date::text            AS "Date",
                i.invoice_no                    AS "Invoice No",
                p.party_name                    AS "Party",
                COALESCE(p.gstin,'—')         AS "GSTIN",
                COALESCE(p.city,'—')          AS "City",
                ROUND(i.total_amount, 2)        AS "Taxable (₹)",
                ROUND(i.total_tax, 2)           AS "GST (₹)",
                ROUND(i.grand_total, 2)         AS "Total (₹)"
            FROM invoices i
            JOIN parties p ON p.id = i.party_id
            WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(p.gstin, '') != ''
              AND COALESCE(i.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(i.status, '')) != 'CANCELLED'
            ORDER BY i.invoice_date DESC
        """, {"fd": fd, "td": td})

        if not rows:
            st.info("No B2B invoices found (no parties with GSTIN in this period).")
            return
        df = _df(rows)
        for c in ["Taxable (₹)", "GST (₹)", "Total (₹)"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        m1, m2, m3 = st.columns(3)
        m1.metric("B2B Invoices", str(len(df)))
        m2.metric("Taxable",      f"₹{df['Taxable (₹)'].sum():,.0f}")
        m3.metric("GST",          f"₹{df['GST (₹)'].sum():,.0f}")
        st.dataframe(df, use_container_width=True, hide_index=True)
        _print_btn(df, f"B2B {fd} to {td}", "gst_b2b")

    elif gst_view == "B2C Detail":
        rows = _q("""
            SELECT
                i.invoice_date::text            AS "Date",
                i.invoice_no                    AS "Invoice No",
                COALESCE(p.party_name,
                  o.patient_name, '—')         AS "Customer",
                COALESCE(p.city, '—')          AS "City",
                ROUND(i.total_amount, 2)        AS "Taxable (₹)",
                ROUND(i.total_tax, 2)           AS "GST (₹)",
                ROUND(i.grand_total, 2)         AS "Total (₹)"
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            LEFT JOIN orders o  ON o.id = ANY(i.order_ids::uuid[])
            WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(p.gstin, '') = ''
              AND COALESCE(i.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(i.status, '')) != 'CANCELLED'
            ORDER BY i.invoice_date DESC
            LIMIT 500
        """, {"fd": fd, "td": td})

        if not rows:
            st.info("No B2C invoices found in this period.")
            return
        df = _df(rows)
        for c in ["Taxable (₹)", "GST (₹)", "Total (₹)"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        m1, m2, m3 = st.columns(3)
        m1.metric("B2C Invoices", str(len(df)))
        m2.metric("Taxable",      f"₹{df['Taxable (₹)'].sum():,.0f}")
        m3.metric("GST",          f"₹{df['GST (₹)'].sum():,.0f}")
        st.dataframe(df, use_container_width=True, hide_index=True)
        _print_btn(df, f"B2C {fd} to {td}", "gst_b2c")

    elif gst_view == "HSN-wise":
        rows = _q("""
            SELECT
                COALESCE(p.hsn_code, '—')       AS "HSN Code",
                p.main_group                     AS "Category",
                ROUND(SUM(ol.quantity), 0)       AS "Qty",
                ROUND(SUM(ol.unit_price * ol.quantity), 2) AS "Taxable (₹)",
                ROUND(AVG(ol.gst_percent), 1)   AS "GST %%",
                ROUND(SUM(ol.unit_price * ol.quantity
                      * COALESCE(ol.gst_percent, 0) / 100), 2) AS "GST Amt (₹)",
                ROUND(SUM(ol.unit_price * ol.quantity
                      * (1 + COALESCE(ol.gst_percent, 0) / 100)), 2) AS "Total (₹)"
            FROM order_lines ol
            JOIN orders o   ON o.id = ol.order_id
            JOIN products p ON p.id = ol.product_id
            WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(o.is_deleted, FALSE) = FALSE
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
              AND o.status NOT IN ('CANCELLED')
            GROUP BY p.hsn_code, p.main_group
            ORDER BY "Taxable (₹)" DESC
        """, {"fd": fd, "td": td})

        if not rows:
            st.info("No order lines found in this period.")
            return
        df = _df(rows)
        for c in ["Qty", "Taxable (₹)", "GST Amt (₹)", "Total (₹)"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("HSN Codes",   str(len(df)))
        m2.metric("Taxable",     f"₹{df['Taxable (₹)'].sum():,.0f}")
        m3.metric("GST",         f"₹{df['GST Amt (₹)'].sum():,.0f}")
        m4.metric("Total",       f"₹{df['Total (₹)'].sum():,.0f}")
        st.dataframe(df, use_container_width=True, hide_index=True)
        _print_btn(df, f"HSN-wise GST {fd} to {td}", "gst_hsn")




# ── TAB 11: AUDIT DASHBOARD ───────────────────────────────────────────────────
def _tab_audit():
    st.caption("Full audit trail — all ledger entries, reversals, payments by user/date/party")

    c1, c2, c3 = st.columns(3)
    with c1: fd, td = _date_filter("aud")
    with c2: pty = _party_picker("aud")
    with c3:
        user_filter = st.text_input("Filter by user", key="aud_user",
                                     placeholder="username…")
        entry_type  = st.multiselect("Entry types",
            ["INVOICE","PAYMENT","DISCOUNT","REVERSAL","CHALLAN"],
            default=[], key="aud_types")

    try:
        from modules.billing.services.reversal_service import (
            get_audit_ledger, get_reversal_summary, ensure_reversal_columns,
        )
        ensure_reversal_columns()
    except ImportError:
        st.error("Reversal service not available — deploy modules/billing/services/reversal_service.py")
        return

    # ── Summary metrics ───────────────────────────────────────────────────
    summary_rows = _q("""
        SELECT
            entry_type,
            COUNT(*) AS count,
            ROUND(SUM(debit), 2)  AS total_debit,
            ROUND(SUM(credit), 2) AS total_credit
        FROM party_ledger
        WHERE entry_date BETWEEN %(fd)s AND %(td)s
        GROUP BY entry_type ORDER BY entry_type
    """, {"fd": fd, "td": td})

    if summary_rows:
        rev_count = sum(r["count"] for r in summary_rows
                        if r["entry_type"] == "REVERSAL")
        pay_count = sum(r["count"] for r in summary_rows
                        if r["entry_type"] == "PAYMENT")
        inv_count = sum(r["count"] for r in summary_rows
                        if r["entry_type"] == "INVOICE")
        total_cr  = sum(float(r["total_credit"] or 0) for r in summary_rows)
        total_dr  = sum(float(r["total_debit"]  or 0) for r in summary_rows)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Invoices",   str(inv_count))
        m2.metric("Payments",   str(pay_count))
        m3.metric("Reversals",  str(rev_count),
                  delta="⚠️" if rev_count > 0 else None,
                  delta_color="inverse" if rev_count > 0 else "off")
        m4.metric("Total DR",   f"₹{total_dr:,.0f}")
        m5.metric("Total CR",   f"₹{total_cr:,.0f}")

    # ── Reversal highlight section ────────────────────────────────────────
    rev_rows = get_reversal_summary(str(fd), str(td))
    if rev_rows:
        st.markdown(
            f"<div style='background:#1a0a0a;border:1px solid #ef4444;"
            f"border-radius:8px;padding:10px 14px;margin:8px 0'>"
            f"<span style='color:#fca5a5;font-weight:700'>"
            f"⚠️ {len(rev_rows)} Reversal(s) in this period</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        rev_df = _df(rev_rows)
        for c in ["Amount (₹)"]:
            if c in rev_df.columns:
                rev_df[c] = pd.to_numeric(rev_df[c], errors="coerce").fillna(0)
        st.dataframe(rev_df, use_container_width=True, hide_index=True,
            column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")})

    # ── Full audit ledger ─────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Full Ledger Entries**")

    pname = "" if pty == "All Parties" else pty
    rows  = get_audit_ledger(
        party_name  = pname,
        date_from   = str(fd),
        date_to     = str(td),
        user_id     = user_filter,
        entry_types = entry_type or None,
        limit       = 500,
    )

    if not rows:
        st.info("No ledger entries found for these filters.")
        return

    df = _df(rows)
    for c in ["Debit (₹)", "Credit (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Style reversals differently
    def _highlight(row):
        if row.get("Is Reversal"):
            return ["background-color:#2d0a0a;color:#fca5a5"] * len(row)
        if row.get("Type") == "INVOICE":
            return ["background-color:#0a1628"] * len(row)
        return [""] * len(row)

    st.caption(f"{len(df)} entries  ·  Reversals highlighted in red")

    # Drop internal columns from display
    display_cols = ["Date","Type","Party","Ref No","Debit (₹)","Credit (₹)","Narration","User","Is Reversal"]
    display_df   = df[[c for c in display_cols if c in df.columns]]

    st.dataframe(display_df, use_container_width=True, hide_index=True,
        column_config={
            "Debit (₹)":  st.column_config.NumberColumn(format="₹%.2f"),
            "Credit (₹)": st.column_config.NumberColumn(format="₹%.2f"),
            "Is Reversal": st.column_config.CheckboxColumn("Reversal?"),
        })
    _print_btn(display_df, f"Audit Log {fd} to {td}", "aud")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def _tab_sequence_audit() -> None:
    """Document number sequence gap audit report."""
    import datetime as _sad

    st.markdown("### 🔢 Document Number Sequence Audit")
    st.caption(
        "All detected gaps in document sequences. Every gap must be explained. "
        "Automatically scanned daily — manual scan available below."
    )

    sc1, _ = st.columns([2, 5])
    if sc1.button("🔍 Scan Now", key="rpt_gap_scan", type="primary"):
        with st.spinner("Scanning..."):
            results = _q("SELECT * FROM detect_all_doc_gaps(%(fy)s, 'manual_scan')",
                         {"fy": _sad.date.today().strftime("%y%m")}) or []
        total = sum(int(r.get("gaps_found") or 0) for r in results)
        if total:
            st.success(f"✅ Scan complete — {total} new gap(s) found.")
        else:
            st.success("✅ No new gaps — all sequences clean.")
        st.rerun()

    with st.expander("🔍 Filters", expanded=True):
        rf1, rf2 = st.columns(2)
        _dtypes = rf1.multiselect(
            "Document Type",
            ["ORDER","CHALLAN","INVOICE","DISPATCH","PAYMENT","CN","DN"],
            default=["ORDER","CHALLAN","INVOICE","DISPATCH","CN","DN"],
            key="rpt_gap_types",
        )
        _sts = rf2.multiselect(
            "Status",
            ["UNEXPLAINED","EXPLAINED","VOIDED","TEST_DATA","SYSTEM_ERROR"],
            default=["UNEXPLAINED","EXPLAINED","VOIDED","TEST_DATA","SYSTEM_ERROR"],
            key="rpt_gap_status",
        )

    summary = _q("""
        SELECT doc_type, COUNT(*) AS gap_events,
               SUM(gap_count) AS missing_numbers,
               SUM(CASE WHEN status='UNEXPLAINED' THEN 1 ELSE 0 END) AS unexplained
        FROM doc_number_gap_log
        GROUP BY doc_type ORDER BY unexplained DESC, doc_type
    """) or []

    if summary:
        cols = st.columns(max(len(summary), 1))
        for i, s in enumerate(summary):
            unex = int(s.get("unexplained") or 0)
            cols[i].metric(
                s["doc_type"],
                f"{s['gap_events']} gap(s)",
                f"⚠️ {unex} unexplained" if unex else "✅ All explained",
                delta_color="inverse" if unex else "normal",
            )
        st.markdown("---")

    rows = _q("""
        SELECT doc_type, doc_prefix, fy, gap_from, gap_to, gap_count,
               status, reason, detected_at::date::text AS detected_on,
               detected_by, resolution_note
        FROM doc_number_gap_log
        WHERE (%(types)s = ARRAY[]::text[] OR doc_type = ANY(%(types)s))
          AND (%(sts)s   = ARRAY[]::text[] OR status   = ANY(%(sts)s))
        ORDER BY CASE status WHEN 'UNEXPLAINED' THEN 0 ELSE 1 END,
                 detected_at DESC
        LIMIT 200
    """, {"types": _dtypes or [], "sts": _sts or []}) or []

    if not rows:
        st.info("No gaps for the selected filters.")
        return

    STATUS_ICON = {"UNEXPLAINED":"❌","EXPLAINED":"✅","VOIDED":"🔵",
                   "TEST_DATA":"⚪","SYSTEM_ERROR":"⚠️"}

    for r in rows:
        pad = 5 if r["doc_type"] == "DISPATCH" else 4
        g_range = f"{r['doc_prefix']}/{r['fy']}/{str(r['gap_from']).zfill(pad)}"
        if r["gap_to"] > r["gap_from"]:
            g_range += f" → {r['doc_prefix']}/{r['fy']}/{str(r['gap_to']).zfill(pad)} ({r['gap_count']})"
        icon  = STATUS_ICON.get(r["status"], "?")
        note  = r.get("resolution_note") or r.get("reason") or "—"
        color = "#ef4444" if r["status"] == "UNEXPLAINED" else "#334155"
        st.markdown(
            f"<div style='border-left:3px solid {color};padding:4px 12px;margin:2px 0;"
            f"font-size:0.80rem'>"
            f"<b style='color:#e2e8f0'>{icon} {r['doc_type']} · {g_range}</b>"
            f"  <span style='color:#64748b'>{r['status']} · {r['detected_on']}</span><br>"
            f"<span style='color:#94a3b8'>{note}</span></div>",
            unsafe_allow_html=True,
        )


def render_reports():
    st.markdown("## 📊 Reports")

    tabs = st.tabs([
        "📒 Ledger",
        "📦 Product Sales",
        "📋 Columnar",
        "⏰ Credit Days",
        "🚚 Challan Register",
        "🏭 Stock Value",
        "💰 Party Outstanding",
        "📅 Aging Report",
        "💵 Cash Flow",
        "🧾 GST Summary",
        "🔍 Audit Trail",
        "🔢 Sequence Audit",
    ])
    with tabs[0]:  _tab_ledger()
    with tabs[1]:  _tab_product_sales()
    with tabs[2]:  _tab_columnar()
    with tabs[3]:  _tab_credit_days()
    with tabs[4]:  _tab_challan_register()
    with tabs[5]:  _tab_stock_value()
    with tabs[6]:  _tab_outstanding()
    with tabs[7]:  _tab_aging()
    with tabs[8]:  _tab_cashflow()
    with tabs[9]:  _tab_gst()
    with tabs[10]: _tab_audit()
    with tabs[11]: _tab_sequence_audit()
