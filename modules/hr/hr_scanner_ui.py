"""
modules/hr/hr_scanner_ui.py
============================
Additional HR UI tabs for:
  - 📱 Scanner Setup  (assign barcodes to staff, show QR to open scanner)
  - ⚙️ Production Log (view stage log, order history)
  - 🔑 Admin Clearance (clear unclosed days without needing Flask admin page)

Plug into hr_ui.py render_hr() by adding these tabs.
"""
import streamlit as st
import streamlit.components.v1 as _stc
import pandas as pd
import socket
from datetime import date


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.10"


# ══════════════════════════════════════════════════════════════════════════════
# TAB — SCANNER SETUP
# ══════════════════════════════════════════════════════════════════════════════

def _tab_scanner_setup():
    st.caption("Assign barcodes to staff · Print stage barcode sheet · Get PWA QR")

    from modules.hr.hr_scanner_engine import ensure_scanner_schema, save_staff_barcode
    from modules.hr.hr_engine import get_all_employees, ensure_hr_schema
    ensure_hr_schema()
    ensure_scanner_schema()

    ip   = _local_ip()
    port = 8502
    url  = f"http://{ip}:{port}"

    # ── PWA QR code ───────────────────────────────────────────────────────────
    st.markdown("### 📱 Scanner App")
    col1, col2 = st.columns([1, 2])

    with col1:
        try:
            import qrcode, io, base64
            qr = qrcode.QRCode(version=None,
                               error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=5, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            st.markdown(
                f"<img src='data:image/png;base64,{b64}' style='width:160px;border-radius:8px'>",
                unsafe_allow_html=True
            )
        except Exception:
            st.code(url)

    with col2:
        st.markdown(f"""
**Staff — open this once on mobile:**

`{url}`

Then tap **⋮ → Add to Home Screen**

After that, tap the **Parakh Scanner** icon on home screen — no typing needed.

**Admin panel:** `{url}/admin`
        """)

    st.markdown("---")

    # ── Assign staff barcodes ─────────────────────────────────────────────────
    st.markdown("### 👤 Staff Barcode Assignment")
    st.caption("Each staff member needs a unique barcode on their ID card. Set it here.")

    emps = get_all_employees()
    if not emps:
        st.info("No employees yet — add them in the Employees tab first.")
        return

    # Show current assignment status
    bc_rows = _q("""
        SELECT id::text, name, emp_code, role,
               COALESCE(staff_barcode,'') AS staff_barcode
        FROM employees WHERE is_active=TRUE ORDER BY name
    """)

    assigned   = [r for r in bc_rows if r["staff_barcode"]]
    unassigned = [r for r in bc_rows if not r["staff_barcode"]]

    c1, c2 = st.columns(2)
    c1.metric("Assigned",   len(assigned))
    c2.metric("Unassigned", len(unassigned), delta_color="inverse" if unassigned else "off")

    if unassigned:
        st.warning(f"⚠️ {len(unassigned)} staff member(s) have no barcode assigned.")

    with st.expander("➕ Assign / Update Staff Barcode", expanded=bool(unassigned)):
        emp_opts = {f"{e['name']} ({e['role']})": e for e in emps}
        chosen   = st.selectbox("Select Employee", list(emp_opts.keys()), key="bc_emp_sel")
        emp      = emp_opts[chosen]
        cur_match = next((r for r in bc_rows if r.get("id") == emp.get("id")), {})
        cur_bc   = cur_match.get("staff_barcode") or ""

        st.caption(f"Current barcode: `{cur_bc}`" if cur_bc else "No barcode assigned yet")

        new_bc = st.text_input(
            "New Barcode Value",
            value=cur_bc or f"EMP{emp.get('emp_code','001')}",
            key="bc_new_val",
            help="This will be printed on their ID card and scanned at check-in"
        )

        if st.button("💾 Save Barcode", type="primary", key="bc_save",
                     disabled=not new_bc.strip()):
            ok = save_staff_barcode(emp["id"], new_bc.strip())
            if ok:
                st.success(f"✅ Barcode `{new_bc.strip()}` assigned to {emp['name']}")
                st.rerun()
            else:
                st.error("❌ Save failed — barcode may already be in use.")

    # Staff barcode table
    st.markdown("**Staff Barcode List**")
    df = pd.DataFrame(bc_rows)[["name","role","emp_code","staff_barcode"]]
    df.columns = ["Name","Role","Code","Barcode"]
    st.dataframe(df, hide_index=True, use_container_width=True)

    # Print barcodes button
    if st.button("🖨️ Print Staff ID Barcodes", key="print_staff_bc"):
        _print_staff_barcode_sheet(bc_rows)

    st.markdown("---")

    # ── Stage barcode sheet ───────────────────────────────────────────────────
    st.markdown("### ⚙️ Stage Barcode Sheet")
    st.caption("Print once · Laminate · Stick at each lab station")

    from modules.hr.hr_scanner_engine import STAGE_PRINT_BARCODES
    for code, label in STAGE_PRINT_BARCODES:
        st.markdown(
            f"<span style='font-size:.8rem;color:#64748b'>`{code}`</span>"
            f"&nbsp;&nbsp;<b>{label}</b>",
            unsafe_allow_html=True
        )

    if st.button("🖨️ Print Stage Barcode Sheet", type="primary", key="print_stages"):
        _print_stage_barcode_sheet()


def _print_staff_barcode_sheet(bc_rows: list):
    """Generate printable HTML with staff barcodes."""
    from modules.printing.patient_card_printer import barcode_svg as _bsvg

    cards = ""
    for r in bc_rows:
        bc = r.get("staff_barcode","")
        if not bc:
            continue
        svg = _bsvg(bc, width=200, height=40)
        cards += f"""
        <div class="card">
            <div class="name">{r['name']}</div>
            <div class="role">{r.get('role','')} · {r.get('emp_code','')}</div>
            {svg}
            <div class="bc-text">{bc}</div>
        </div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 10mm; }}
body {{ font-family: Arial; display: flex; flex-wrap: wrap; gap: 8mm; padding: 5mm; }}
.card {{ border: 1px solid #000; border-radius: 6px; padding: 6mm 8mm;
         width: 80mm; text-align: center; break-inside: avoid; }}
.name {{ font-size: 13pt; font-weight: 800; margin-bottom: 2mm; }}
.role {{ font-size: 8pt; color: #64748b; margin-bottom: 3mm; }}
.bc-text {{ font-size: 7pt; font-family: monospace; margin-top: 2mm; color: #475569; }}
</style>
</head><body>{cards}
<script>window.onload=function(){{window.print();}}</script>
</body></html>"""

    out = r"C:\Users\Vinay\Desktop\staff_barcodes.html"
    try:
        open(out, "w", encoding="utf-8").write(html)
        st.success(f"✅ Saved to Desktop: staff_barcodes.html — will open and print")
        import os; os.startfile(out)
    except Exception as e:
        st.download_button("⬇ Download Staff Barcode Sheet", html.encode(),
                           "staff_barcodes.html", "text/html")


def _print_stage_barcode_sheet():
    """Generate printable A4 stage barcode sheet for lab walls."""
    from modules.printing.patient_card_printer import barcode_svg as _bsvg
    from modules.hr.hr_scanner_engine import STAGE_PRINT_BARCODES

    # Also include system barcodes
    all_items = (
        [("SYS:CHECKIN",  "📍 CHECK IN  — scan when arriving"),
         ("SYS:CHECKOUT", "🏁 CHECK OUT — scan when leaving"),
         ("SYS:CLEARANCE","🔑 ADMIN CLEARANCE")] +
        [(code, label) for code, label in STAGE_PRINT_BARCODES]
    )

    rows_html = ""
    for bc_val, label in all_items:
        svg  = _bsvg(bc_val, width=280, height=48)
        cat  = "sys" if bc_val.startswith("SYS:") else "stage"
        bg   = "#e8f4fd" if cat == "sys" else "#f0fdf4"
        rows_html += f"""
        <tr style="background:{bg}">
            <td class="label">{label}</td>
            <td class="bc">{svg}<div class="bc-text">{bc_val}</div></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 12mm; }}
body {{ font-family: Arial; }}
h1 {{ font-size: 16pt; font-weight: 800; margin-bottom: 6mm; text-align: center; }}
h2 {{ font-size: 9pt; color: #64748b; text-align: center; margin-bottom: 8mm; }}
table {{ width: 100%; border-collapse: collapse; }}
tr {{ border-bottom: 1px solid #e2e8f0; }}
td {{ padding: 5mm 4mm; vertical-align: middle; }}
.label {{ font-size: 12pt; font-weight: 700; width: 45%; }}
.bc {{ text-align: left; }}
.bc-text {{ font-size: 7pt; font-family: monospace; color: #94a3b8; margin-top: 1mm; }}
.section {{ background: #0f172a; color: white; padding: 3mm 4mm;
            font-size: 9pt; font-weight: 700; letter-spacing: .05em; }}
</style>
</head><body>
<h1>Parakh Eye Care — Scanner Barcodes</h1>
<h2>Print · Laminate · Stick at each station</h2>
<table>
<tr><td colspan="2" class="section">ATTENDANCE — Stick near entrance door</td></tr>
<tr style="background:#e8f4fd">
    <td class="label">📍 CHECK IN<br><span style="font-size:9pt;color:#475569">Scan when arriving</span></td>
    <td class="bc">{_bsvg("SYS:CHECKIN", width=280, height=48)}<div class="bc-text">SYS:CHECKIN</div></td>
</tr>
<tr style="background:#fee2e2">
    <td class="label">🏁 CHECK OUT<br><span style="font-size:9pt;color:#475569">Scan when leaving</span></td>
    <td class="bc">{_bsvg("SYS:CHECKOUT", width=280, height=48)}<div class="bc-text">SYS:CHECKOUT</div></td>
</tr>
<tr><td colspan="2" class="section">PRODUCTION STAGES — Stick at each workstation</td></tr>
{''.join(f"""
<tr style="background:{'#f0fdf4' if i%2==0 else '#ffffff'}">
    <td class="label">{label}</td>
    <td class="bc">{_bsvg(code, width=280, height=48)}<div class="bc-text">{code}</div></td>
</tr>""" for i,(code,label) in enumerate(STAGE_PRINT_BARCODES))}
<tr><td colspan="2" class="section">ADMIN — Keep with manager only</td></tr>
<tr style="background:#fef9c3">
    <td class="label">🔑 Admin Clearance<br><span style="font-size:9pt;color:#475569">Override unclosed day</span></td>
    <td class="bc">{_bsvg("SYS:CLEARANCE", width=280, height=48)}<div class="bc-text">SYS:CLEARANCE</div></td>
</tr>
</table>
<script>window.onload=function(){{window.print();}}</script>
</body></html>"""

    out = r"C:\Users\Vinay\Desktop\stage_barcodes.html"
    try:
        open(out, "w", encoding="utf-8").write(html)
        st.success("✅ Saved to Desktop: stage_barcodes.html")
        import os; os.startfile(out)
    except Exception as e:
        st.download_button("⬇ Download Stage Barcode Sheet", html.encode(),
                           "stage_barcodes.html", "text/html")


# ══════════════════════════════════════════════════════════════════════════════
# TAB — PRODUCTION LOG
# ══════════════════════════════════════════════════════════════════════════════

def _tab_production_log():
    st.caption("View production stage movements for any order or date")

    from modules.hr.hr_scanner_engine import ensure_scanner_schema
    ensure_scanner_schema()

    sub1, sub2 = st.tabs(["📦 By Order", "📅 By Date"])

    with sub1:
        order_no = st.text_input("Order No", placeholder="R/2627/0013",
                                  key="pl_order_no")
        if order_no.strip():
            rows = _q("""
                SELECT stage_label, emp_name,
                       scanned_at::text AS scanned_at
                FROM production_stage_log
                WHERE order_no = %s
                ORDER BY scanned_at
            """, (order_no.strip(),))

            if rows:
                st.markdown(f"**{len(rows)} stage(s) for `{order_no}`**")
                for r in rows:
                    t = str(r.get("scanned_at",""))[:16]
                    st.markdown(
                        f"🕐 `{t}` &nbsp;→&nbsp; "
                        f"**{r.get('stage_label','')}** "
                        f"<span style='color:#64748b'>by {r.get('emp_name','')}</span>",
                        unsafe_allow_html=True
                    )
            else:
                st.info("No stage records for this order.")

    with sub2:
        log_date = st.date_input("Date", value=date.today(), key="pl_date")
        rows = _q("""
            SELECT order_no, stage_label, emp_name,
                   scanned_at::text AS scanned_at
            FROM production_stage_log
            WHERE DATE(scanned_at) = %s
            ORDER BY scanned_at DESC
            LIMIT 200
        """, (log_date,))

        if rows:
            df = pd.DataFrame(rows)
            df["time"] = df["scanned_at"].str[11:16]
            st.dataframe(
                df[["time","order_no","stage_label","emp_name"]].rename(columns={
                    "time": "Time", "order_no": "Order",
                    "stage_label": "Stage", "emp_name": "By"
                }),
                hide_index=True, use_container_width=True
            )
        else:
            st.info("No activity on this date.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB — ADMIN CLEARANCE
# ══════════════════════════════════════════════════════════════════════════════

def _tab_admin_clearance():
    st.caption("Clear unclosed attendance days so staff can log in next morning")

    from modules.hr.hr_scanner_engine import (
        ensure_scanner_schema, get_unclosed_staff, admin_clear_unclosed
    )
    ensure_scanner_schema()

    rows = get_unclosed_staff()

    if not rows:
        st.success("✅ All staff have properly checked out. No clearance needed.")
        return

    st.warning(f"⚠️ {len(rows)} staff member(s) did not check out:")
    st.markdown("---")

    for r in rows:
        c1, c2, c3 = st.columns([2, 1, 1])
        c1.markdown(
            f"**{r['name']}** &nbsp;"
            f"<span style='color:#64748b'>{r['role']}</span><br>"
            f"<span style='color:#f59e0b;font-size:.8rem'>"
            f"Date: {r['log_date']}  Check-in: {str(r.get('check_in_time',''))[:16]}"
            f"</span>",
            unsafe_allow_html=True
        )
        key_reason = f"cl_reason_{r['log_id']}"
        reason = c2.text_input("Reason", key=key_reason,
                                placeholder="Late night, forgot, etc.")
        if c3.button("✅ Clear", key=f"cl_btn_{r['log_id']}", type="primary"):
            ok = admin_clear_unclosed(
                r["id"], r["log_date"],
                st.session_state.get("user_name","Admin"),
                st.session_state.get(key_reason,"")
            )
            if ok:
                st.success(f"✅ {r['name']} cleared for {r['log_date']}")
                st.rerun()
            else:
                st.error("Clearance failed — check DB")
        st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# LAUNCHER — call this to add scanner tabs to render_hr()
# ══════════════════════════════════════════════════════════════════════════════

def render_scanner_tabs():
    """
    Call from hr_ui.render_hr() to add scanner tabs.

    Usage in hr_ui.py:
        from modules.hr.hr_scanner_ui import render_scanner_tabs
        # add to tabs list and with blocks
    """
    tabs = st.tabs([
        "📱 Scanner Setup",
        "⚙️ Production Log",
        "🔑 Admin Clearance",
    ])
    with tabs[0]: _tab_scanner_setup()
    with tabs[1]: _tab_production_log()
    with tabs[2]: _tab_admin_clearance()
