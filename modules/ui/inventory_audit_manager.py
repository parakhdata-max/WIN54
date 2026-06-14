"""
Inventory Audit Manager
Physical barcode scan audit for frames and contact lenses.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


AUDIT_TYPES = {
    "FRAMES": {
        "label": "Frames / Sunglasses",
        "group_like": ["frame", "sunglass"],
        "scan_mode": "item",
    },
    "CONTACT_LENSES": {
        "label": "Contact Lenses",
        "group_like": ["contact lens"],
        "scan_mode": "qty",
    },
}


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or {})


def _w(sql, params=None):
    from modules.sql_adapter import run_write
    return run_write(sql, params or {})


def _user() -> str:
    try:
        from modules.security.roles import current_user_name
        return current_user_name() or "System"
    except Exception:
        return "System"


def _now_label() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _clean_code(value: str) -> str:
    return str(value or "").strip().upper()


def _ensure_schema() -> None:
    _w(
        """
        CREATE TABLE IF NOT EXISTS inventory_audit_sessions (
            id uuid PRIMARY KEY,
            audit_no text UNIQUE NOT NULL,
            audit_type text NOT NULL,
            location text,
            stock_basis text NOT NULL DEFAULT 'TOTAL',
            status text NOT NULL DEFAULT 'OPEN',
            started_by text,
            started_at timestamptz NOT NULL DEFAULT NOW(),
            completed_at timestamptz,
            remarks text
        );

        CREATE TABLE IF NOT EXISTS inventory_audit_scans (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES inventory_audit_sessions(id) ON DELETE CASCADE,
            barcode text NOT NULL,
            scan_qty numeric NOT NULL DEFAULT 1,
            scanned_at timestamptz NOT NULL DEFAULT NOW(),
            scanned_by text,
            scan_status text NOT NULL DEFAULT 'SCANNED',
            matched_stock_id uuid,
            notes text
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_audit_scans_session
            ON inventory_audit_scans(session_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_audit_scans_barcode
            ON inventory_audit_scans(UPPER(TRIM(barcode)));
        """
    )


def _load_sessions(limit=40) -> list[dict]:
    return _q(
        """
        SELECT id::text AS id, audit_no, audit_type, location, stock_basis,
               status, started_by, started_at, completed_at, remarks
        FROM inventory_audit_sessions
        ORDER BY started_at DESC
        LIMIT %(limit)s
        """,
        {"limit": int(limit)},
    )


def _create_session(audit_type: str, location: str, stock_basis: str, remarks: str = "") -> str:
    sid = str(uuid.uuid4())
    audit_no = f"IA-{_now_label()}"
    _w(
        """
        INSERT INTO inventory_audit_sessions
            (id, audit_no, audit_type, location, stock_basis, started_by, remarks)
        VALUES (%(id)s::uuid, %(audit_no)s, %(audit_type)s, %(location)s,
                %(stock_basis)s, %(started_by)s, %(remarks)s)
        """,
        {
            "id": sid,
            "audit_no": audit_no,
            "audit_type": audit_type,
            "location": location.strip() or None,
            "stock_basis": stock_basis,
            "started_by": _user(),
            "remarks": remarks.strip() or None,
        },
    )
    return sid


def _finish_session(session_id: str) -> None:
    _w(
        """
        UPDATE inventory_audit_sessions
        SET status='COMPLETED', completed_at=NOW()
        WHERE id=%(id)s::uuid
        """,
        {"id": session_id},
    )


def _reopen_session(session_id: str) -> None:
    _w(
        """
        UPDATE inventory_audit_sessions
        SET status='OPEN', completed_at=NULL
        WHERE id=%(id)s::uuid
        """,
        {"id": session_id},
    )


def _expected_rows(session: dict) -> list[dict]:
    cfg = AUDIT_TYPES.get(session["audit_type"], AUDIT_TYPES["FRAMES"])
    clauses = []
    params = {}
    for i, pat in enumerate(cfg["group_like"]):
        key = f"g{i}"
        clauses.append(f"LOWER(COALESCE(p.main_group,'')) LIKE %({key})s")
        params[key] = f"%{pat}%"
    group_sql = " OR ".join(clauses) or "TRUE"

    qty_expr = "COALESCE(s.quantity,0)"
    if str(session.get("stock_basis") or "TOTAL").upper() == "FREE":
        qty_expr = "GREATEST(0, COALESCE(s.quantity,0) - COALESCE(s.allocated_qty,0))"

    loc_sql = ""
    location = str(session.get("location") or "").strip()
    if location:
        params["location"] = location
        loc_sql = "AND UPPER(COALESCE(s.location,'')) = UPPER(%(location)s)"

    return _q(
        f"""
        SELECT s.id::text AS stock_id,
               COALESCE(s.batch_no,'') AS batch_no,
               COALESCE(s.barcode,'') AS barcode,
               COALESCE(s.location,'') AS location,
               COALESCE(s.mrp,0)::numeric AS mrp,
               COALESCE(s.quantity,0)::numeric AS db_total_qty,
               COALESCE(s.allocated_qty,0)::numeric AS allocated_qty,
               ({qty_expr})::numeric AS expected_qty,
               COALESCE(p.product_name,'') AS product_name,
               COALESCE(p.brand,'') AS brand,
               COALESCE(p.main_group,'') AS main_group
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE COALESCE(s.is_active, TRUE) = TRUE
          AND ({group_sql})
          AND ({qty_expr}) > 0
          {loc_sql}
        ORDER BY p.main_group, p.product_name, s.batch_no
        """,
        params,
    )


def _available_locations(audit_type: str) -> list[dict]:
    cfg = AUDIT_TYPES.get(audit_type, AUDIT_TYPES["FRAMES"])
    clauses = []
    params = {}
    for i, pat in enumerate(cfg["group_like"]):
        key = f"g{i}"
        clauses.append(f"LOWER(COALESCE(p.main_group,'')) LIKE %({key})s")
        params[key] = f"%{pat}%"
    group_sql = " OR ".join(clauses) or "TRUE"
    return _q(
        f"""
        SELECT TRIM(COALESCE(s.location,'')) AS location,
               COUNT(*)::int AS item_count,
               SUM(COALESCE(s.quantity,0))::numeric AS total_qty,
               SUM(GREATEST(0, COALESCE(s.quantity,0)-COALESCE(s.allocated_qty,0)))::numeric AS free_qty
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE COALESCE(s.is_active, TRUE) = TRUE
          AND ({group_sql})
          AND COALESCE(s.quantity,0) > 0
          AND TRIM(COALESCE(s.location,'')) <> ''
        GROUP BY TRIM(COALESCE(s.location,''))
        ORDER BY TRIM(COALESCE(s.location,''))
        """,
        params,
    )


def _find_stock_for_scan(audit_type: str, barcode: str) -> str | None:
    code = _clean_code(barcode)
    if not code:
        return None
    cfg = AUDIT_TYPES.get(audit_type, AUDIT_TYPES["FRAMES"])
    clauses = []
    params = {"code": code}
    for i, pat in enumerate(cfg["group_like"]):
        key = f"g{i}"
        clauses.append(f"LOWER(COALESCE(p.main_group,'')) LIKE %({key})s")
        params[key] = f"%{pat}%"
    group_sql = " OR ".join(clauses) or "TRUE"
    rows = _q(
        f"""
        SELECT s.id::text AS stock_id
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE COALESCE(s.is_active, TRUE) = TRUE
          AND ({group_sql})
          AND (
              UPPER(TRIM(COALESCE(s.batch_no,''))) = %(code)s
              OR UPPER(TRIM(COALESCE(s.barcode,''))) = %(code)s
          )
        ORDER BY s.updated_at DESC NULLS LAST, s.id
        LIMIT 1
        """,
        params,
    )
    return rows[0]["stock_id"] if rows else None


def _add_scan(session: dict, barcode: str, qty: float = 1) -> tuple[bool, str]:
    code = _clean_code(barcode)
    if not code:
        return False, "Scan barcode first"
    qty = max(float(qty or 1), 1)
    stock_id = _find_stock_for_scan(session["audit_type"], code)
    _w(
        """
        INSERT INTO inventory_audit_scans
            (id, session_id, barcode, scan_qty, scanned_by, scan_status, matched_stock_id)
        VALUES (%(id)s::uuid, %(session_id)s::uuid, %(barcode)s, %(qty)s,
                %(user)s, %(status)s, %(stock_id)s::uuid)
        """,
        {
            "id": str(uuid.uuid4()),
            "session_id": session["id"],
            "barcode": code,
            "qty": qty,
            "user": _user(),
            "status": "MATCHED" if stock_id else "UNKNOWN",
            "stock_id": stock_id,
        },
    )
    return True, "Matched DB" if stock_id else "Not found in DB"


def _scans(session_id: str) -> list[dict]:
    return _q(
        """
        SELECT id::text AS id, barcode, scan_qty::numeric AS scan_qty,
               scanned_at, scanned_by, scan_status,
               matched_stock_id::text AS matched_stock_id
        FROM inventory_audit_scans
        WHERE session_id=%(id)s::uuid
        ORDER BY scanned_at DESC
        """,
        {"id": session_id},
    )


def _delete_scan(scan_id: str) -> None:
    _w("DELETE FROM inventory_audit_scans WHERE id=%(id)s::uuid", {"id": scan_id})


def _build_report(session: dict) -> tuple[pd.DataFrame, dict]:
    expected = _expected_rows(session)
    scans = _scans(session["id"])

    expected_by_stock = {r["stock_id"]: r for r in expected}
    alias_to_stock = {}
    for row in expected:
        for code in (row.get("batch_no"), row.get("barcode")):
            c = _clean_code(code)
            if c:
                alias_to_stock[c] = row["stock_id"]

    scanned_qty_by_stock = defaultdict(float)
    scanned_unknown = Counter()
    scan_count_by_code = Counter()
    for scan in scans:
        code = _clean_code(scan.get("barcode"))
        qty = float(scan.get("scan_qty") or 1)
        scan_count_by_code[code] += 1
        sid = scan.get("matched_stock_id") or alias_to_stock.get(code)
        if sid and sid in expected_by_stock:
            scanned_qty_by_stock[sid] += qty
        else:
            scanned_unknown[code] += qty

    rows = []
    for stock_id, exp in expected_by_stock.items():
        expected_qty = float(exp.get("expected_qty") or 0)
        scanned_qty = float(scanned_qty_by_stock.get(stock_id, 0))
        diff = scanned_qty - expected_qty
        if scanned_qty == expected_qty:
            status = "TALLIED"
        elif scanned_qty == 0:
            status = "MISSING_PHYSICAL"
        elif diff < 0:
            status = "SHORT"
        else:
            status = "EXCESS_SCAN"
        rows.append({
            "Status": status,
            "Product": exp.get("product_name", ""),
            "Brand": exp.get("brand", ""),
            "Group": exp.get("main_group", ""),
            "SKU/Batch": exp.get("batch_no", ""),
            "Barcode": exp.get("barcode", ""),
            "Location": exp.get("location", ""),
            "MRP": float(exp.get("mrp") or 0),
            "DB Qty": expected_qty,
            "Physical Scan Qty": scanned_qty,
            "Difference": diff,
        })

    for code, qty in scanned_unknown.items():
        rows.append({
            "Status": "EXTRA_PHYSICAL_NOT_IN_DB",
            "Product": "",
            "Brand": "",
            "Group": "",
            "SKU/Batch": code,
            "Barcode": code,
            "Location": "",
            "MRP": 0.0,
            "DB Qty": 0.0,
            "Physical Scan Qty": float(qty),
            "Difference": float(qty),
        })

    dupes = {k: v for k, v in scan_count_by_code.items() if v > 1}
    df = pd.DataFrame(rows)
    if not df.empty:
        order = {
            "MISSING_PHYSICAL": 0,
            "SHORT": 1,
            "EXTRA_PHYSICAL_NOT_IN_DB": 2,
            "EXCESS_SCAN": 3,
            "TALLIED": 9,
        }
        df["_sort"] = df["Status"].map(order).fillna(8)
        df = df.sort_values(["_sort", "Product", "SKU/Batch"]).drop(columns=["_sort"])

    metrics = {
        "expected_lines": len(expected),
        "scans": len(scans),
        "tallied": int((df["Status"] == "TALLIED").sum()) if not df.empty else 0,
        "variance": int((df["Status"] != "TALLIED").sum()) if not df.empty else 0,
        "duplicates": len(dupes),
        "duplicate_codes": dupes,
    }
    return df, metrics


def _render_print_report(session: dict, df: pd.DataFrame, metrics: dict) -> None:
    rows_html = ""
    if df.empty:
        rows_html = "<tr><td colspan='10'>No data</td></tr>"
    else:
        for _, r in df.iterrows():
            status = str(r.get("Status") or "")
            color = "#dc2626" if status != "TALLIED" else "#047857"
            rows_html += (
                "<tr>"
                f"<td style='color:{color};font-weight:800'>{status}</td>"
                f"<td>{r.get('Product','')}</td>"
                f"<td>{r.get('Brand','')}</td>"
                f"<td>{r.get('SKU/Batch','')}</td>"
                f"<td>{r.get('Barcode','')}</td>"
                f"<td>{r.get('Location','')}</td>"
                f"<td style='text-align:right'>{float(r.get('DB Qty') or 0):g}</td>"
                f"<td style='text-align:right'>{float(r.get('Physical Scan Qty') or 0):g}</td>"
                f"<td style='text-align:right'>{float(r.get('Difference') or 0):g}</td>"
                f"<td style='text-align:right'>{float(r.get('MRP') or 0):.0f}</td>"
                "</tr>"
            )
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<style>
@page {{ size: A4 landscape; margin: 8mm; }}
body {{ font-family: Arial, sans-serif; font-size: 9pt; color:#111; }}
h1 {{ font-size: 16pt; margin:0 0 2mm; }}
.meta {{ display:flex; gap:8mm; margin:2mm 0 4mm; font-size:8pt; }}
.metric {{ border:1px solid #999; padding:2mm 3mm; font-weight:800; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border:1px solid #999; padding:1.4mm; vertical-align:top; }}
th {{ background:#111827; color:#fff; }}
.no-print {{ margin:12px 0; }}
@media print {{ .no-print {{ display:none }} }}
</style></head><body>
<div class='no-print'><button onclick='window.print()'>Print Audit Report</button></div>
<h1>Inventory Audit Report</h1>
<div class='meta'>
  <div><b>Audit:</b> {session.get('audit_no')}</div>
  <div><b>Type:</b> {AUDIT_TYPES.get(session.get('audit_type'),{}).get('label', session.get('audit_type'))}</div>
  <div><b>Location:</b> {session.get('location') or 'All'}</div>
  <div><b>Basis:</b> {session.get('stock_basis')}</div>
  <div><b>Status:</b> {session.get('status')}</div>
</div>
<div class='meta'>
  <div class='metric'>Expected: {metrics['expected_lines']}</div>
  <div class='metric'>Scans: {metrics['scans']}</div>
  <div class='metric'>Tallied: {metrics['tallied']}</div>
  <div class='metric'>Variance: {metrics['variance']}</div>
  <div class='metric'>Duplicate Codes: {metrics['duplicates']}</div>
</div>
<table>
<thead><tr>
  <th>Status</th><th>Product</th><th>Brand</th><th>SKU/Batch</th><th>Barcode</th>
  <th>Location</th><th>DB Qty</th><th>Physical</th><th>Diff</th><th>MRP</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""
    b64 = base64.b64encode(html.encode("utf-8")).decode()
    components.html(
        f"<script>(function(){{var raw=atob('{b64}');var buf=new Uint8Array(raw.length);for(var i=0;i<raw.length;i++)buf[i]=raw.charCodeAt(i);var b=new Blob([buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank');}})();</script>",
        height=0,
    )


def render_inventory_audit_manager() -> None:
    _ensure_schema()
    st.markdown("### Inventory Audit Manager")
    st.caption("Scan physical barcodes and compare against DB stock. No stock is adjusted from this screen.")

    sessions = _load_sessions()
    left, right = st.columns([1, 2])

    with left:
        st.markdown("#### New Audit")
        audit_label = st.selectbox("Audit type", [v["label"] for v in AUDIT_TYPES.values()], key="ia_new_type")
        audit_type = next(k for k, v in AUDIT_TYPES.items() if v["label"] == audit_label)
        stock_basis = st.radio(
            "DB quantity basis",
            ["TOTAL", "FREE"],
            index=0,
            horizontal=True,
            help="TOTAL = all DB stock. FREE = quantity minus allocated_qty.",
        )
        locations = _available_locations(audit_type)
        location_labels = ["All locations"] + [
            f"{r['location']} · {int(float(r.get('total_qty') or 0)):g} qty · {int(r.get('item_count') or 0)} line(s)"
            for r in locations
        ]
        location_by_label = {"All locations": ""}
        location_by_label.update({label: r["location"] for label, r in zip(location_labels[1:], locations)})
        location_label = st.selectbox(
            "Location / rack / box filter",
            location_labels,
            index=0,
            key=f"ia_location_pick_{audit_type}",
            help="Values come directly from inventory_stock.location. Choose All for complete audit.",
        )
        location = location_by_label.get(location_label, "")
        if locations:
            st.caption(f"{len(locations)} DB location(s) available for this audit type.")
        else:
            st.caption("No saved DB locations found for this audit type.")
        remarks = st.text_area("Remarks", height=70, placeholder="Optional")
        if st.button("Start Audit Session", type="primary", use_container_width=True):
            sid = _create_session(audit_type, location, stock_basis, remarks)
            st.session_state["inventory_audit_session_id"] = sid
            st.rerun()

    with right:
        if not sessions:
            st.info("No audit sessions yet. Start one from the left.")
            return
        current_id = st.session_state.get("inventory_audit_session_id") or sessions[0]["id"]
        labels = [
            f"{s['audit_no']} · {AUDIT_TYPES.get(s['audit_type'],{}).get('label',s['audit_type'])} · {s['status']} · {s.get('location') or 'All'}"
            for s in sessions
        ]
        id_by_label = {label: s["id"] for label, s in zip(labels, sessions)}
        default_idx = next((i for i, s in enumerate(sessions) if s["id"] == current_id), 0)
        chosen_label = st.selectbox("Audit session", labels, index=default_idx, key="ia_session_pick")
        session_id = id_by_label[chosen_label]
        st.session_state["inventory_audit_session_id"] = session_id

    session = next((s for s in sessions if s["id"] == session_id), None) or _load_sessions(1)[0]
    _render_session(session)


def _render_session(session: dict) -> None:
    st.markdown("---")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Audit No", session.get("audit_no", ""))
    m2.metric("Type", AUDIT_TYPES.get(session.get("audit_type"), {}).get("label", session.get("audit_type")))
    m3.metric("Location", session.get("location") or "All")
    m4.metric("Status", session.get("status"))

    is_open = session.get("status") == "OPEN"
    if is_open:
        st.markdown("#### Scan")
        with st.form("ia_scan_form", clear_on_submit=True):
            c1, c2, c3 = st.columns([3, 1, 1])
            barcode = c1.text_input("Scan barcode", placeholder="Scan / type barcode and press Enter")
            qty_default = 1.0
            qty = c2.number_input("Qty", min_value=1.0, value=qty_default, step=1.0)
            submitted = c3.form_submit_button("Record", type="primary", use_container_width=True)
        if submitted:
            ok, msg = _add_scan(session, barcode, qty)
            if ok:
                if msg == "Matched DB":
                    st.success(f"{_clean_code(barcode)} recorded")
                else:
                    st.warning(f"{_clean_code(barcode)} recorded — {msg}")
                st.rerun()
            else:
                st.error(msg)

    df, metrics = _build_report(session)
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Expected Lines", metrics["expected_lines"])
    s2.metric("Scans", metrics["scans"])
    s3.metric("Tallied", metrics["tallied"])
    s4.metric("Variance Lines", metrics["variance"])
    s5.metric("Duplicate Codes", metrics["duplicates"])

    if metrics["duplicate_codes"]:
        st.warning(
            "Duplicate scans: "
            + ", ".join(f"{k} x{v}" for k, v in list(metrics["duplicate_codes"].items())[:12])
        )

    actions = st.columns([1, 1, 1, 2])
    with actions[0]:
        if is_open and st.button("Finish / Lock", use_container_width=True):
            _finish_session(session["id"])
            st.rerun()
        elif not is_open and st.button("Reopen", use_container_width=True):
            _reopen_session(session["id"])
            st.rerun()
    with actions[1]:
        if st.button("Print Report", use_container_width=True):
            _render_print_report(session, df, metrics)
    with actions[2]:
        csv = df.to_csv(index=False).encode("utf-8") if not df.empty else b""
        st.download_button("Download CSV", csv, file_name=f"{session['audit_no']}.csv", mime="text/csv", use_container_width=True)

    tab1, tab2 = st.tabs(["Variance Report", "Recent Scans"])
    with tab1:
        if df.empty:
            st.info("No expected stock or scans found for this session.")
        else:
            status_filter = st.multiselect(
                "Status filter",
                sorted(df["Status"].dropna().unique().tolist()),
                default=[s for s in sorted(df["Status"].dropna().unique().tolist()) if s != "TALLIED"],
            )
            view = df[df["Status"].isin(status_filter)] if status_filter else df
            st.dataframe(view, use_container_width=True, hide_index=True)
    with tab2:
        scans = _scans(session["id"])
        if scans:
            for row in scans[:100]:
                c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
                c1.markdown(f"**{row['barcode']}**")
                c2.caption(f"Qty {float(row.get('scan_qty') or 1):g}")
                c3.caption(f"{row.get('scan_status')} · {row.get('scanned_at')}")
                if is_open and c4.button("Delete", key=f"ia_del_{row['id']}"):
                    _delete_scan(row["id"])
                    st.rerun()
        else:
            st.info("No scans yet.")
