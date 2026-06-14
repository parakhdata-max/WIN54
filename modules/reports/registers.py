# -*- coding: utf-8 -*-
"""
modules/reports/registers.py
==============================
Business Registers -- Tally-equivalent day books and registers.

All registers share:
  - Date range filter (presets + custom)
  - Party / account filter
  - Daily / Monthly / Yearly grouping
  - Print + CSV export

Registers:
  1.  Sales Register         -- invoices raised, line-wise
  2.  Purchase Register      -- purchase invoices
  3.  Payment Receipt Book   -- all receipts (party-wise, mode-wise)
  4.  Payment Disbursement   -- all outgoing payments
  5.  Cash Book              -- cash in / out daily
  6.  Bank Book              -- bank account statement
  7.  Party Ledger           -- individual party account
  8.  Debtors Register       -- all debtors outstanding
  9.  Creditors Register     -- all creditors outstanding
  10. Order Register         -- all orders by party / status
  11. Challan Register       -- all challans
  12. Stock Movement         -- stock in / out
  13. Journal Register       -- all JV entries
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta
import calendar
import html as _html
import re
import urllib.parse
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or ()) or []


def _w(sql, params=None):
    from modules.sql_adapter import run_write
    run_write(sql, params or {})


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _fmt(v):
    try: return f"₹{float(v or 0):,.2f}"
    except: return "₹0.00"


def _safe_key(v):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(v or "doc"))[:80]


def _shop_payment_info():
    try:
        from modules.settings.shop_master import get_unit_info
        return get_unit_info("retail") or {}
    except Exception:
        return {}


def _upi_qr_html(upi_id, shop_name, amount=0.0, ref=""):
    upi_id = str(upi_id or "").strip()
    if not upi_id:
        return ""
    try:
        import qrcode, io, base64
        params = {
            "pa": upi_id,
            "pn": str(shop_name or "DV Optical"),
            "cu": "INR",
        }
        try:
            amt = round(float(amount or 0), 2)
            if amt > 0:
                params["am"] = f"{amt:.2f}"
        except Exception:
            pass
        if ref:
            params["tn"] = str(ref)
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=3,
            border=2,
        )
        qr.add_data("upi://pay?" + urllib.parse.urlencode(params))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return (
            "<div class='payqr'>"
            "<div class='payqr-img' style=\"background-image:url(data:image/png;base64,{})\"></div>"
            "<div class='payqr-title'>Scan to Pay</div>"
            "<div class='payqr-upi'>{}</div>"
            "</div>"
        ).format(b64, _html.escape(upi_id))
    except Exception:
        return "<div class='payqr'><b>UPI:</b><br>{}</div>".format(_html.escape(upi_id))


def _clean_mobile(mobile):
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) > 12:
        digits = digits[-10:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if 10 < len(digits) < 12:
        return ""
    if len(digits) < 10:
        return ""
    return digits


def _wa_anchor(label, url):
    safe_url = _html.escape(str(url or ""), quote=True)
    safe_label = _html.escape(str(label or "WhatsApp"))
    st.markdown(
        "<a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\" "
        "style=\"display:block;text-align:center;padding:0.58rem 0.75rem;"
        "border-radius:0.5rem;background:#25d366;color:white;font-weight:700;"
        "text-decoration:none;border:1px solid #1da851\">{label}</a>".format(
            url=safe_url, label=safe_label
        ),
        unsafe_allow_html=True,
    )


def _wa_url(mobile, message):
    mob = _clean_mobile(mobile)
    if not mob:
        return ""
    msg = str(message or "")
    if len(msg) > 1800:
        msg = msg[:1750] + "\n\n...[message shortened]"
    try:
        from modules.wa_hub import wa_link
        return wa_link(mob, msg)
    except Exception:
        return f"https://wa.me/{mob}?text={urllib.parse.quote(msg)}"


def _lookup_mobile_for_party(party_name):
    name = str(party_name or "").strip()
    if not name or name == "—":
        return ""
    rows = _q("""
        SELECT mobile FROM (
            SELECT COALESCE(mobile,'') AS mobile, 1 AS ord
            FROM parties WHERE party_name = %(p)s
            UNION ALL
            SELECT COALESCE(patient_mobile,'') AS mobile, 2 AS ord
            FROM orders WHERE COALESCE(patient_name, party_name) = %(p)s
            UNION ALL
            SELECT COALESCE(mobile,'') AS mobile, 3 AS ord
            FROM patients WHERE master_name = %(p)s
        ) x
        WHERE COALESCE(mobile,'') <> ''
        ORDER BY ord
        LIMIT 1
    """, {"p": name})
    return rows[0]["mobile"] if rows else ""


def _save_mobile_for_party(party_name, mobile):
    name = str(party_name or "").strip()
    mob12 = _clean_mobile(mobile)
    if not name or name == "—":
        return False, "No party/customer name available to save against."
    if not mob12:
        return False, "Enter a valid 10-digit mobile number."
    mob10 = mob12[-10:]
    try:
        rows = _q("SELECT id::text FROM parties WHERE party_name = %(p)s LIMIT 1", {"p": name})
        if rows:
            _w("UPDATE parties SET mobile = %(m)s WHERE id = %(id)s::uuid", {"m": mob10, "id": rows[0]["id"]})
            return True, "Mobile saved in CRM party master."
        rows = _q("SELECT id::text FROM patients WHERE master_name = %(p)s LIMIT 1", {"p": name})
        if rows:
            _w("UPDATE patients SET mobile = %(m)s WHERE id = %(id)s::uuid", {"m": mob10, "id": rows[0]["id"]})
            return True, "Mobile saved in patient master."
        updated = _w("""
            UPDATE orders
            SET patient_mobile = %(m)s
            WHERE COALESCE(patient_name, party_name) = %(p)s
              AND COALESCE(patient_mobile,'') = ''
        """, {"m": mob10, "p": name})
        return True, "Mobile saved against existing order/customer history."
    except Exception as exc:
        return False, f"Mobile save failed: {exc}"


def _mobile_input_save_panel(key, party_name, current_mobile=""):
    saved_mobile = _lookup_mobile_for_party(party_name)
    base = saved_mobile or current_mobile or st.session_state.get(f"{key}_mobile_override", "")
    st.text_input(
        "WhatsApp mobile",
        value=_clean_mobile(base)[-10:] if _clean_mobile(base) else str(base or ""),
        key=f"{key}_mobile_override",
        placeholder="10-digit mobile",
    )
    entered = st.session_state.get(f"{key}_mobile_override", "")
    if party_name:
        if st.button("Save mobile to DB", key=f"{key}_save_mobile", use_container_width=True):
            ok, msg = _save_mobile_for_party(party_name, entered)
            if ok:
                st.success(msg)
            else:
                st.warning(msg)
    return entered or saved_mobile or current_mobile


def _render_wa_action(label, mobile, message, key, party_name=None):
    crm_mobile = _lookup_mobile_for_party(party_name) if party_name else ""
    manual = st.session_state.get(f"{key}_mobile_override", "")
    mobile = manual or crm_mobile or mobile
    url = _wa_url(mobile, message)
    if url:
        _wa_anchor(label, url)
        with st.popover("Link / copy", use_container_width=True):
            st.text_input("WhatsApp link", url, key=f"{key}_url_copy")
            st.text_area("Message", str(message or ""), height=150, key=f"{key}_msg_copy")
    else:
        mobile = _mobile_input_save_panel(key, party_name, mobile)
        url = _wa_url(mobile, message)
        if url:
            _wa_anchor(label, url)
            with st.popover("Link / copy", use_container_width=True):
                st.text_input("WhatsApp link", url, key=f"{key}_url_copy")
                st.text_area("Message", str(message or ""), height=150, key=f"{key}_msg_copy")
            return
        st.button(label, disabled=True, key=f"{key}_disabled",
                  use_container_width=True, help="No valid 10-digit mobile number found.")
        st.caption("Enter a valid 10-digit WhatsApp mobile number and save it.")
        with st.popover("Copy message", use_container_width=True):
            st.text_area("Message", str(message or ""), height=150, key=f"{key}_copy")


def _open_print_html(html, filename, key):
    if st.button("🖨️ Print / Save PDF", key=key, use_container_width=True):
        try:
            from modules.printing.print_opener import open_html_print
            if "window.print" not in str(html):
                btn = (
                    "<div style='text-align:center;padding:10px;background:#f3f4f6' "
                    "class='no-print'><button onclick='window.print()' "
                    "style='padding:10px 18px;border:0;border-radius:6px;"
                    "background:#2563eb;color:white;font-weight:700'>"
                    "Print / Save PDF</button></div>"
                )
                if "<body" in html:
                    html = re.sub(r"(<body[^>]*>)", r"\1" + btn, html, count=1, flags=re.I)
                else:
                    html = btn + html
            path = open_html_print(html, filename)
            st.success(f"Opened print document: {path}")
        except Exception as exc:
            st.error(f"Print open failed: {exc}")


def _direct_print_html(html, filename, key, label="🖨️ Direct Print"):
    if st.button(label, key=key, use_container_width=True):
        try:
            from modules.printing.direct_print import spool_html_to_printer
            from modules.printing.print_opener import open_html_print
            from modules.printing.printer_config import load_printer_settings

            ok, msg = spool_html_to_printer(html, job_name=filename.replace(".html", ""))
            if ok:
                st.success(msg)
            else:
                st.warning(f"Direct print unavailable: {msg}")
                if bool(load_printer_settings().get("html_fallback", True)):
                    path = open_html_print(html, filename)
                    st.info(f"Opened HTML standby: {path}")
        except Exception as exc:
            st.error(f"Direct print failed: {exc}")


def _render_register_grid(display, key, select_col=None, select_state_key=None,
                          column_config=None):
    """Render a register table with optional visible single-row selection."""
    if select_col and select_state_key and select_col in display.columns:
        grid = display.copy()
        if st.session_state.get(select_state_key) in (None, "") and not grid.empty:
            st.session_state[select_state_key] = str(grid.iloc[0].get(select_col) or "")
        selected_ref = str(st.session_state.get(select_state_key, "") or "")
        if "✓" not in grid.columns:
            grid.insert(0, "✓", grid[select_col].astype(str).eq(selected_ref))
        else:
            grid["✓"] = grid[select_col].astype(str).eq(selected_ref)

        cfg = {"✓": st.column_config.CheckboxColumn("✓", help="Select this document")}
        if column_config:
            cfg.update(column_config)
        disabled_cols = [c for c in grid.columns if c != "✓"]

        try:
            edited = st.data_editor(
                grid,
                width="stretch",
                hide_index=True,
                column_config=cfg,
                disabled=disabled_cols,
                key=f"{key}_grid",
            )
            checked = edited.loc[edited["✓"].fillna(False), select_col].astype(str).tolist()
            if checked:
                newly_checked = [x for x in checked if x != selected_ref]
                picked = newly_checked[-1] if newly_checked else checked[-1]
                if picked and st.session_state.get(select_state_key) != picked:
                    st.session_state[select_state_key] = picked
                    st.rerun()
            return edited.drop(columns=["✓"], errors="ignore")
        except TypeError:
            pass

    try:
        event = st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config=column_config,
            key=f"{key}_grid",
            on_select="rerun",
            selection_mode="single-row",
        )
        rows = []
        try:
            rows = list(event.selection.rows)
        except Exception:
            try:
                rows = list(event.get("selection", {}).get("rows", []))
            except Exception:
                rows = []
        if rows and select_col and select_state_key and select_col in display.columns:
            selected = display.iloc[int(rows[0])].get(select_col)
            if selected not in (None, ""):
                st.session_state[select_state_key] = str(selected)
        return event
    except TypeError:
        return st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config=column_config,
        )


def _save_share_html(html, filename):
    root = Path(__file__).resolve().parents[2] / "generated_docs" / "whatsapp" / "html"
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[/\\:*?"<>|]+', "-", str(filename or "document.html")).strip(". ")
    if not safe.lower().endswith(".html"):
        safe += ".html"
    path = root / safe
    path.write_text(str(html or ""), encoding="utf-8")
    return path, path.resolve().as_uri()


def _render_uploaded_purchase_invoice_download(ref_no, party_name="", key="upl_inv"):
    """Show supplier uploaded invoice/challan image download when a saved path exists."""
    if not ref_no:
        return
    rows = []
    try:
        rows = _q("""
            SELECT
                COALESCE(NULLIF(invoice_file_path,''), NULLIF(notes,''), '') AS file_path,
                COALESCE(NULLIF(invoice_no,''), NULLIF(challan_no,''), '') AS doc_no
            FROM purchase_acknowledgements
            WHERE (
                    invoice_no = %(rno)s
                 OR challan_no = %(rno)s
                 OR supplier_invoice_no = %(rno)s
                 OR 'PA-' || id::text = %(rno)s
            )
              AND (%(party)s = '' OR supplier_name ILIKE %(party_like)s)
              AND COALESCE(NULLIF(invoice_file_path,''), NULLIF(notes,''), '') <> ''
            ORDER BY created_at DESC
            LIMIT 1
        """, {
            "rno": str(ref_no),
            "party": str(party_name or ""),
            "party_like": f"%{str(party_name or '')}%",
        })
    except Exception:
        rows = []

    if not rows:
        return

    raw_path = str(rows[0].get("file_path") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    if not path.exists():
        st.caption(f"Uploaded invoice file path saved, but file was not found: {raw_path}")
        return

    try:
        import mimetypes
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        st.download_button(
            "⬇️ Download uploaded supplier invoice image",
            data=path.read_bytes(),
            file_name=path.name,
            mime=mime,
            key=f"{key}_download_{_safe_key(ref_no)}",
            use_container_width=True,
        )
    except Exception as exc:
        st.warning(f"Could not prepare uploaded invoice download: {exc}")


# ── LAN document server (Tally-style) ────────────────────────────────────────
_LAN_SERVER_DIR  = Path(__file__).resolve().parents[2] / "generated_docs" / "whatsapp" / "html"
_LAN_SERVER_PORT = 8765          # primary; falls back to 8766, 8767, 8768
_LAN_SERVER_ACTUAL_PORT = None   # set after bind succeeds


def _lan_base_url(for_whatsapp: bool = True) -> str:
    """Return base http://host:PORT using the machine LAN IP and confirmed port.

    Reads the port from builtins first so it survives Streamlit reruns
    where the module-level global may have reset to None.

    WhatsApp often treats raw 192.168.x.x text as a phone number. For WhatsApp
    messages, expose the same LAN IP through nip.io so the text is recognized
    as a normal website link while still resolving back to this machine.
    """
    import socket, builtins
    port = (
        getattr(builtins, "_registers_lan_server_port", None)
        or _LAN_SERVER_ACTUAL_PORT
        or _LAN_SERVER_PORT
    )
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    host = f"{ip}.nip.io" if for_whatsapp and ip != "127.0.0.1" else ip
    return f"http://{host}:{port}"


def _ensure_lan_server():
    """Start a background HTTP server once per process (daemon thread).

    Tries ports 8765-8768 in order.  Only marks the server as running
    after a successful bind so dead links are never sent via WhatsApp.
    """
    import threading, http.server, socketserver, builtins
    if getattr(builtins, "_registers_lan_server_running", False):
        # Restore module-level port in case this module was reloaded
        global _LAN_SERVER_ACTUAL_PORT
        if _LAN_SERVER_ACTUAL_PORT is None:
            _LAN_SERVER_ACTUAL_PORT = getattr(builtins, "_registers_lan_server_port", None)
        return
    _LAN_SERVER_DIR.mkdir(parents=True, exist_ok=True)

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(_LAN_SERVER_DIR), **kw)
        def log_message(self, fmt, *args):
            pass  # suppress console noise

    bound_port = None
    httpd_ref  = [None]
    for port in (8765, 8766, 8767, 8768):
        try:
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("", port), _Handler)
            httpd_ref[0] = httpd
            bound_port   = port
            break
        except OSError:
            continue

    if bound_port is None:
        # All ports busy — server cannot start; links will silently degrade
        return

    _LAN_SERVER_ACTUAL_PORT = bound_port
    # Persist port in builtins so it survives Streamlit module reloads
    builtins._registers_lan_server_port = bound_port

    def _run():
        httpd_ref[0].serve_forever()

    t = threading.Thread(target=_run, daemon=True, name="registers_lan_server")
    t.start()
    builtins._registers_lan_server_running = True


def _save_and_lan_url(html, filename) -> str:
    """Save HTML to shared folder and return LAN http:// URL.

    Raises RuntimeError if the LAN server could not bind to any port,
    so callers can show a warning instead of sending a dead WhatsApp link.
    """
    import urllib.parse as _up, builtins
    _ensure_lan_server()
    if not getattr(builtins, "_registers_lan_server_running", False):
        raise RuntimeError(
            "LAN document server could not start (ports 8765-8768 all busy). "
            "Close other applications using those ports and restart Streamlit."
        )
    _LAN_SERVER_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[/\\:*?"<>|]+', "-", str(filename or "document.html")).strip(". ")
    if not safe.lower().endswith(".html"):
        safe += ".html"
    (_LAN_SERVER_DIR / safe).write_text(str(html or ""), encoding="utf-8")
    encoded = _up.quote(safe)
    return f"{_lan_base_url(for_whatsapp=True)}/{encoded}"


def _wa_with_print_link(label, mobile, message, html, filename, key, party_name=None):
    manual = st.session_state.get(f"{key}_mobile_override", "")
    if manual:
        mobile = manual
    if not html:
        st.button(label, disabled=True, use_container_width=True,
                  help="Print HTML is not available.")
        return
    try:
        lan_url = _save_and_lan_url(html, filename)
        msg = f"{message}\n\nView / Print\n{lan_url}"
        _render_wa_action(label, mobile, msg, key, party_name=party_name)
        st.caption(f"Link: {lan_url}")
    except Exception as exc:
        st.error(f"Could not create print link: {exc}")


def _simple_doc_html(title, subtitle, rows_html, totals_html=""):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page{{size:A4;margin:10mm}}
body{{font-family:Arial,sans-serif;color:#111;background:#fff;margin:0}}
.wrap{{max-width:760px;margin:0 auto;padding:18px}}
.top{{display:flex;justify-content:space-between;border-bottom:2px solid #111;padding-bottom:10px;margin-bottom:14px}}
.title{{font-size:22px;font-weight:800}}
.sub{{font-size:12px;color:#555;line-height:1.5}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}}
th{{background:#111;color:#fff;text-align:left;padding:7px}}
td{{border-bottom:1px solid #ddd;padding:7px;vertical-align:top}}
.r{{text-align:right}}
.tot{{margin-left:auto;width:320px;margin-top:14px;border:1px solid #ddd;padding:10px;font-size:13px}}
.tot div{{display:flex;justify-content:space-between;padding:3px 0}}
.grand{{font-weight:800;border-top:1px solid #111;margin-top:5px;padding-top:7px!important}}
.paybox{{margin-top:12px;border:1px solid #ddd;padding:10px;display:flex;justify-content:space-between;gap:14px;align-items:center}}
.paybox-title{{font-size:13px;font-weight:800;margin-bottom:4px}}
.paybox-sub{{font-size:11px;color:#555;line-height:1.4}}
.payqr{{text-align:center;min-width:86px}}
.payqr-img{{width:68px;height:68px;margin:0 auto;background-size:contain;background-repeat:no-repeat;
             -webkit-print-color-adjust:exact;print-color-adjust:exact;color-adjust:exact}}
.payqr-title{{font-size:10px;color:#555;font-weight:700;margin-top:2px}}
.payqr-upi{{font-size:9px;font-family:monospace;color:#111;word-break:break-all;max-width:120px}}
.print{{position:fixed;top:8px;right:8px}}
@media print{{.print{{display:none}}}}
</style></head><body>
<button class="print" onclick="window.print()">Print</button>
<div class="wrap">
  <div class="top"><div><div class="title">{_html.escape(str(title))}</div>
  <div class="sub">{subtitle}</div></div></div>
  {rows_html}
  {totals_html}
</div></body></html>"""


def _sales_invoice_action_drawer(df, key="sr_doc"):
    if df.empty or "Invoice No" not in df.columns:
        return
    with st.expander("🔎 Open Sales Invoice / Print / WhatsApp", expanded=False):
        invoice_nos = [str(x) for x in df["Invoice No"].dropna().tolist()]
        sel_key = f"{key}_sel"
        if invoice_nos and st.session_state.get(sel_key) not in invoice_nos:
            st.session_state[sel_key] = invoice_nos[0]
        inv_no = st.selectbox("Sales invoice", invoice_nos, key=sel_key)
        if not inv_no:
            return
        row = _q("""
            SELECT i.id::text, i.invoice_no, i.invoice_date::text, i.grand_total,
                   COALESCE(i.amount_paid,0) AS amount_paid,
                   COALESCE(i.balance_due, i.grand_total) AS balance_due,
                   COALESCE(i.payment_status,'UNPAID') AS payment_status,
                   COALESCE(p.party_name, o.party_name, o.patient_name, '') AS party_name,
                   COALESCE(p.mobile, o.patient_mobile, '') AS mobile,
                   c.challan_no
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name, o2.patient_mobile
                FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
            ) o ON TRUE
            LEFT JOIN challans c ON c.id = i.challan_id
            WHERE i.invoice_no = %(ino)s
            LIMIT 1
        """, {"ino": inv_no})
        if not row:
            st.warning("Invoice not found.")
            return
        inv = row[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Party", inv.get("party_name") or "—")
        c2.metric("Invoice", _fmt(inv.get("grand_total")))
        c3.metric("Paid", _fmt(inv.get("amount_paid")))
        c4.metric("Balance", _fmt(inv.get("balance_due")))

        try:
            from modules.billing.smart_print import render_smart_invoice
            html_doc = render_smart_invoice(inv_no, return_html=True)
        except Exception as exc:
            html_doc = ""
            st.caption(f"Invoice print template unavailable: {exc}")
        msg = (
            f"Hello {inv.get('party_name') or ''},\n"
            f"Invoice {inv_no} dated {inv.get('invoice_date') or ''} is {_fmt(inv.get('grand_total'))}.\n"
            f"Paid: {_fmt(inv.get('amount_paid'))}. Balance: {_fmt(inv.get('balance_due'))}."
        )

        a1, a2, a3, a4 = st.columns(4)
        with a1:
            if html_doc:
                _open_print_html(html_doc, f"sales_invoice_{inv_no}.html", f"{key}_print_{_safe_key(inv_no)}")
        with a2:
            _render_wa_action(
                "📲 WhatsApp Invoice",
                inv.get("mobile"),
                msg,
                f"{key}_wa_{_safe_key(inv_no)}",
                party_name=inv.get("party_name"),
            )
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                inv.get("mobile"),
                msg,
                html_doc,
                f"sales_invoice_{inv_no}.html",
                f"{key}_wa_print_{_safe_key(inv_no)}",
                party_name=inv.get("party_name"),
            )
        with a4:
            st.caption(f"Status: {inv.get('payment_status') or '—'}")
            if inv.get("challan_no"):
                st.caption(f"Challan: {inv.get('challan_no')}")


def _purchase_invoice_action_drawer(df, key="pr_doc"):
    if df.empty or "Invoice No" not in df.columns:
        return
    with st.expander("🔎 Open Purchase Invoice / Print / WhatsApp", expanded=False):
        invoice_nos = [str(x) for x in df["Invoice No"].dropna().tolist()]
        sel_key = f"{key}_sel"
        if invoice_nos and st.session_state.get(sel_key) not in invoice_nos:
            st.session_state[sel_key] = invoice_nos[0]
        inv_no = st.selectbox("Purchase invoice", invoice_nos, key=sel_key)
        if not inv_no:
            return
        head = _q("""
            SELECT pi.invoice_no, pi.supplier_invoice_no, pi.invoice_date::text,
                   pi.supplier_name, pi.subtotal, pi.gst_amount, pi.invoice_total,
                   COALESCE(pi.amount_paid,0) AS amount_paid,
                   COALESCE(pi.balance_due, pi.invoice_total) AS balance_due,
                   COALESCE(pi.payment_status,'UNPAID') AS payment_status,
                   COALESCE(p.mobile,'') AS mobile
            FROM purchase_invoices pi
            LEFT JOIN parties p ON p.id::text = pi.supplier_id
            WHERE pi.invoice_no = %(ino)s
            LIMIT 1
        """, {"ino": inv_no})
        if not head:
            st.warning("Purchase invoice not found.")
            return
        pi = head[0]
        lines = _q("""
            SELECT product_name, eye_side, received_qty, actual_price,
                   gst_percent, line_total
            FROM purchase_invoice_lines
            WHERE invoice_no = %(ino)s
            ORDER BY item_no
        """, {"ino": inv_no})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Supplier", pi.get("supplier_name") or "—")
        c2.metric("Invoice", _fmt(pi.get("invoice_total")))
        c3.metric("Paid", _fmt(pi.get("amount_paid")))
        c4.metric("Balance", _fmt(pi.get("balance_due")))

        body_rows = "".join(
            "<tr><td>{}</td><td>{}</td><td class='r'>{}</td><td class='r'>{}</td>"
            "<td class='r'>{}</td><td class='r'>{}</td></tr>".format(
                _html.escape(str(l.get("product_name") or "")),
                _html.escape(str(l.get("eye_side") or "")),
                l.get("received_qty") or 0,
                _fmt(l.get("actual_price")),
                f"{float(l.get('gst_percent') or 0):.2f}%",
                _fmt(l.get("line_total")),
            )
            for l in lines
        )
        table = (
            "<table><thead><tr><th>Product</th><th>Eye</th><th class='r'>Qty</th>"
            "<th class='r'>Rate</th><th class='r'>GST</th><th class='r'>Total</th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )
        totals = (
            "<div class='tot'>"
            f"<div><span>Taxable</span><b>{_fmt(pi.get('subtotal'))}</b></div>"
            f"<div><span>GST</span><b>{_fmt(pi.get('gst_amount'))}</b></div>"
            f"<div class='grand'><span>Total</span><b>{_fmt(pi.get('invoice_total'))}</b></div>"
            "</div>"
        )
        subtitle = (
            f"Supplier: {_html.escape(str(pi.get('supplier_name') or ''))}<br>"
            f"Supplier Inv: {_html.escape(str(pi.get('supplier_invoice_no') or inv_no))}<br>"
            f"Date: {_html.escape(str(pi.get('invoice_date') or ''))}"
        )
        html_doc = _simple_doc_html(f"Purchase Invoice {inv_no}", subtitle, table, totals)
        msg = (
            f"Purchase invoice {inv_no} ({pi.get('supplier_invoice_no') or inv_no}) "
            f"dated {pi.get('invoice_date') or ''}: total {_fmt(pi.get('invoice_total'))}, "
            f"balance {_fmt(pi.get('balance_due'))}."
        )
        a1, a2, a3 = st.columns(3)
        with a1:
            _open_print_html(html_doc, f"purchase_invoice_{inv_no}.html", f"{key}_print_{_safe_key(inv_no)}")
        with a2:
            _render_wa_action(
                "📲 WhatsApp Supplier",
                pi.get("mobile"),
                msg,
                f"{key}_wa_{_safe_key(inv_no)}",
                party_name=pi.get("supplier_name"),
            )
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                pi.get("mobile"),
                msg,
                html_doc,
                f"purchase_invoice_{inv_no}.html",
                f"{key}_wa_print_{_safe_key(inv_no)}",
                party_name=pi.get("supplier_name"),
            )


def _receipt_action_drawer(df, key="prb_doc"):
    if df.empty or "Receipt No" not in df.columns:
        return
    with st.expander("🔎 Open Receipt / Print / WhatsApp", expanded=False):
        receipt_nos = [str(x) for x in df["Receipt No"].dropna().tolist()]
        sel_key = f"{key}_sel"
        if receipt_nos and st.session_state.get(sel_key) not in receipt_nos:
            st.session_state[sel_key] = receipt_nos[0]
        rec_no = st.selectbox("Receipt / payment", receipt_nos, key=sel_key)
        if not rec_no:
            return
        rows = _q("""
            SELECT p.id::text, COALESCE(NULLIF(p.payment_no,''), p.id::text) AS receipt_no,
                   p.payment_date::text, p.amount, COALESCE(NULLIF(p.payment_mode,''), p.method, '') AS mode,
                   COALESCE(p.reference_no,'') AS reference_no,
                   COALESCE(p.remarks,'') AS remarks,
                   COALESCE(p.party_name, ip.party_name, cp.party_name, o.party_name, o.patient_name, '') AS party_name,
                   COALESCE(ip.mobile, cp.mobile, o.patient_mobile, '') AS mobile,
                   COALESCE(i.invoice_no,'') AS invoice_no,
                   COALESCE(c.challan_no,'') AS challan_no
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            LEFT JOIN orders o ON o.id = COALESCE(p.order_id, p.advance_for_order_id)
            WHERE COALESCE(NULLIF(p.payment_no,''), p.id::text) = %(rno)s
            LIMIT 1
        """, {"rno": rec_no})
        if not rows:
            st.warning("Receipt not found.")
            return
        rec = rows[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Party", rec.get("party_name") or "—")
        c2.metric("Amount", _fmt(rec.get("amount")))
        c3.metric("Mode", rec.get("mode") or "—")
        detail = (
            f"Receipt: {_html.escape(str(rec.get('receipt_no') or ''))}<br>"
            f"Party: {_html.escape(str(rec.get('party_name') or ''))}<br>"
            f"Date: {_html.escape(str(rec.get('payment_date') or ''))}<br>"
            f"Mode: {_html.escape(str(rec.get('mode') or ''))}"
        )
        rows_html = (
            "<table><tbody>"
            f"<tr><td>Amount Received</td><td class='r'><b>{_fmt(rec.get('amount'))}</b></td></tr>"
            f"<tr><td>Against Invoice</td><td class='r'>{_html.escape(str(rec.get('invoice_no') or '—'))}</td></tr>"
            f"<tr><td>Against Challan</td><td class='r'>{_html.escape(str(rec.get('challan_no') or '—'))}</td></tr>"
            f"<tr><td>Reference</td><td class='r'>{_html.escape(str(rec.get('reference_no') or '—'))}</td></tr>"
            f"<tr><td>Narration</td><td class='r'>{_html.escape(str(rec.get('remarks') or '—'))}</td></tr>"
            "</tbody></table>"
        )
        html_doc = _simple_doc_html(f"Payment Receipt {rec_no}", detail, rows_html)
        msg = (
            f"Hello {rec.get('party_name') or ''},\n"
            f"Payment received: {_fmt(rec.get('amount'))} on {rec.get('payment_date') or ''}.\n"
            f"Receipt: {rec_no}. Thank you."
        )
        a1, a2, a3 = st.columns(3)
        with a1:
            _open_print_html(html_doc, f"payment_receipt_{rec_no}.html", f"{key}_print_{_safe_key(rec_no)}")
        with a2:
            _render_wa_action(
                "📲 WhatsApp Receipt",
                rec.get("mobile"),
                msg,
                f"{key}_wa_{_safe_key(rec_no)}",
                party_name=rec.get("party_name"),
            )
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                rec.get("mobile"),
                msg,
                html_doc,
                f"payment_receipt_{rec_no}.html",
                f"{key}_wa_print_{_safe_key(rec_no)}",
                party_name=rec.get("party_name"),
            )


def _date_filter(key="reg", default_preset="This month"):
    presets = {
        "Today":         (date.today(), date.today()),
        "This week":     (date.today() - timedelta(days=date.today().weekday()), date.today()),
        "This month":    (date.today().replace(day=1), date.today()),
        "Last month":    ((date.today().replace(day=1) - timedelta(days=1)).replace(day=1),
                          date.today().replace(day=1) - timedelta(days=1)),
        "This quarter":  (date(date.today().year, ((date.today().month-1)//3)*3+1, 1), date.today()),
        "This year":     (date(date.today().year if date.today().month >= 4
                               else date.today().year - 1, 4, 1), date.today()),
        "All time":      (date(1900, 1, 1), date(2099, 12, 31)),
    }
    c1, c2, c3 = st.columns([1, 1, 1])
    preset_key = f"{key}_pre"
    fd_key = f"{key}_fd"
    td_key = f"{key}_td"
    last_preset_key = f"{key}_last_pre"

    idx = list(presets.keys()).index(default_preset) if default_preset in presets else 2
    preset = c3.selectbox("Period", list(presets.keys()), index=idx, key=preset_key)

    # Streamlit date_input preserves widget state by key. When the preset changes,
    # push the new dates into session_state before rendering the date widgets.
    if st.session_state.get(last_preset_key) != preset:
        df, dt = presets[preset]
        st.session_state[fd_key] = df
        st.session_state[td_key] = dt
        st.session_state[last_preset_key] = preset
    elif preset == "All time":
        df, dt = presets[preset]
        if st.session_state.get(fd_key) != df or st.session_state.get(td_key) != dt:
            st.session_state[fd_key] = df
            st.session_state[td_key] = dt

    fd = c1.date_input("From", value=st.session_state.get(fd_key, presets[preset][0]), key=fd_key)
    td = c2.date_input("To",   value=st.session_state.get(td_key, presets[preset][1]), key=td_key)
    return fd, td


def _party_filter(key="reg", label="All Parties", include_patients=False):
    """Filter box + selectbox -- always visible."""
    @st.cache_data(ttl=120, show_spinner=False)
    def _load(scope, active_only=False):
        if active_only and include_patients:
            order_scope = ""
            pay_scope = ""
            inv_scope = ""
            if scope == "Retail":
                order_scope = "AND UPPER(COALESCE(order_type,'')) = 'RETAIL'"
                pay_scope = "AND EXISTS (SELECT 1 FROM orders ox WHERE ((p.order_id IS NOT NULL AND ox.id = p.order_id) OR (p.advance_for_order_id IS NOT NULL AND ox.id = p.advance_for_order_id) OR (i.order_ids IS NOT NULL AND ox.id::text = ANY(i.order_ids::text[])) OR (c.order_ids IS NOT NULL AND ox.id::text = ANY(c.order_ids::text[]))) AND UPPER(COALESCE(ox.order_type,'')) = 'RETAIL')"
                inv_scope = "AND UPPER(COALESCE(o.order_type,'')) = 'RETAIL'"
            elif scope == "Online":
                order_scope = "AND UPPER(COALESCE(order_type,'')) = 'ONLINE'"
                pay_scope = "AND EXISTS (SELECT 1 FROM orders ox WHERE ((p.order_id IS NOT NULL AND ox.id = p.order_id) OR (p.advance_for_order_id IS NOT NULL AND ox.id = p.advance_for_order_id) OR (i.order_ids IS NOT NULL AND ox.id::text = ANY(i.order_ids::text[])) OR (c.order_ids IS NOT NULL AND ox.id::text = ANY(c.order_ids::text[]))) AND UPPER(COALESCE(ox.order_type,'')) = 'ONLINE')"
                inv_scope = "AND UPPER(COALESCE(o.order_type,'')) = 'ONLINE'"
            elif scope == "Wholesale":
                order_scope = "AND UPPER(COALESCE(order_type,'')) NOT IN ('RETAIL','ONLINE')"
                pay_scope = "AND NOT EXISTS (SELECT 1 FROM orders ox WHERE ((p.order_id IS NOT NULL AND ox.id = p.order_id) OR (p.advance_for_order_id IS NOT NULL AND ox.id = p.advance_for_order_id) OR (i.order_ids IS NOT NULL AND ox.id::text = ANY(i.order_ids::text[])) OR (c.order_ids IS NOT NULL AND ox.id::text = ANY(c.order_ids::text[]))) AND UPPER(COALESCE(ox.order_type,'')) IN ('RETAIL','ONLINE'))"
                inv_scope = "AND UPPER(COALESCE(o.order_type,'')) NOT IN ('RETAIL','ONLINE')"
            rows = _q("""
                SELECT party_name FROM (
                    SELECT COALESCE(pt.party_name, o.party_name, o.patient_name) AS party_name
                    FROM invoices i
                    LEFT JOIN parties pt ON pt.id = i.party_id
                    LEFT JOIN LATERAL (
                        SELECT o2.party_name, o2.patient_name, o2.order_type
                        FROM orders o2
                        WHERE o2.id::text = ANY(i.order_ids)
                        LIMIT 1
                    ) o ON TRUE
                    WHERE COALESCE(i.is_deleted,FALSE)=FALSE
                      """ + inv_scope + """
                    UNION
                    SELECT COALESCE(p.party_name, ip.party_name, cp.party_name,
                           (SELECT COALESCE(o.party_name, o.patient_name)
                            FROM orders o
                            WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
                               OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
                               OR (i.order_ids IS NOT NULL AND o.id::text = ANY(i.order_ids::text[]))
                               OR (c.order_ids IS NOT NULL AND o.id::text = ANY(c.order_ids::text[]))
                            LIMIT 1)) AS party_name
                    FROM payments p
                    LEFT JOIN invoices i ON i.id = p.invoice_id
                    LEFT JOIN challans c ON c.id = p.challan_id
                    LEFT JOIN parties ip ON ip.id = i.party_id
                    LEFT JOIN parties cp ON cp.id = c.party_id
                    WHERE NOT COALESCE(p.is_deleted,FALSE)
                      """ + pay_scope + """
                    UNION
                    SELECT COALESCE(patient_name, party_name) AS party_name
                    FROM orders
                    WHERE COALESCE(patient_name, party_name, '') <> ''
                      """ + order_scope + """
                ) x
                WHERE COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            return [r["party_name"] for r in rows]

        if active_only and not include_patients:
            rows = _q("""
                SELECT party_name FROM (
                    SELECT party_name FROM payments
                    WHERE NOT COALESCE(is_deleted,FALSE)
                      AND COALESCE(party_name,'') <> ''
                    UNION
                    SELECT supplier_name AS party_name FROM purchase_invoices
                    WHERE COALESCE(supplier_name,'') <> ''
                      AND NOT COALESCE(is_deleted,FALSE)
                    UNION
                    SELECT party_name FROM parties
                    WHERE COALESCE(is_active,TRUE)=TRUE
                      AND COALESCE(party_name,'') <> ''
                ) x
                WHERE COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            return [r["party_name"] for r in rows]

        if scope in ("All", "Wholesale"):
            rows = _q("""
                SELECT party_name
                FROM parties
                WHERE COALESCE(is_active,TRUE)=TRUE
                  AND COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            names = [r["party_name"] for r in rows]
        else:
            names = []
        if include_patients and scope in ("All", "Retail", "Online"):
            order_scope = ""
            if scope == "Retail":
                order_scope = "AND order_type = 'RETAIL'"
            elif scope == "Online":
                order_scope = "AND order_type = 'ONLINE'"
            pts = _q("""
                SELECT party_name FROM (
                    SELECT COALESCE(patient_name, party_name) AS party_name
                    FROM orders
                    WHERE 1=1
                      """ + order_scope + """
                      AND COALESCE(patient_name, party_name, '') <> ''
                    UNION
                    SELECT COALESCE(master_name,'') AS party_name
                    FROM patients
                    WHERE COALESCE(master_name,'') <> ''
                ) x
                WHERE COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            seen = set(names)
            names += [r["party_name"] for r in pts if r["party_name"] not in seen]
        return names

    scope = "Wholesale"
    if include_patients:
        scope = st.radio(
            "Account type",
            ["All", "Wholesale", "Retail", "Online"],
            horizontal=True,
            key=f"{key}_scope",
        )
    list_mode = st.radio(
        "Account list",
        ["Active ledger only", "All accounts"],
        horizontal=True,
        key=f"{key}_active_only",
        help="Active ledger only shows accounts with invoices, payments, or orders.",
    )
    all_names = _load(scope, list_mode == "Active ledger only")

    term = st.text_input(
        "🔍 Filter party / customer",
        key=f"{key}_finput",
        placeholder="Type to filter...",
    )
    filtered = [n for n in all_names if term.lower() in n.lower()] if term else all_names
    label_text = label or "All Accounts"
    opts = [f"-- {label_text} ({len(filtered)}) --"] + filtered
    party_sel_key = f"{key}_party_sel"
    if st.session_state.get(party_sel_key) not in opts:
        st.session_state[party_sel_key] = opts[0]
    chosen = st.selectbox("Party / Customer", opts, key=party_sel_key)
    return "" if chosen.startswith("--") else chosen


def _grouping(key="reg"):
    return st.radio("Group by", ["Detail","Daily","Monthly","Yearly"],
                    horizontal=True, key=f"{key}_grp")


def _export(df, title, key):
    safe_title = re.sub(r"[^A-Za-z0-9_,-]+", "_", str(title or "export")).strip("_") or "export"
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button(f"⬇ CSV",
        df.to_csv(index=False).encode(),
        file_name=f"{safe_title}.csv",
        mime="text/csv", key=key)
    try:
        xlsx = _df_to_excel_bytes(df, title)
        c2.download_button(
            "⬇ Excel",
            xlsx,
            file_name=f"{safe_title}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key}_xlsx",
        )
    except Exception as exc:
        c2.button("⬇ Excel", disabled=True, key=f"{key}_xlsx_dis",
                  help=f"Excel unavailable: {exc}")
    try:
        pdf = _df_to_pdf_bytes(df, title)
        c3.download_button(
            "⬇ PDF",
            pdf,
            file_name=f"{safe_title}.pdf",
            mime="application/pdf",
            key=f"{key}_pdf",
        )
    except Exception as exc:
        c3.button("⬇ PDF", disabled=True, key=f"{key}_pdf_dis",
                  help=f"PDF unavailable: {exc}")
    html_doc = _df_to_print_html(df, title)
    with c4:
        _direct_print_html(html_doc, f"{safe_title}.html", f"{key}_direct")


def _df_to_excel_bytes(df, title):
    from io import BytesIO
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        sheet = re.sub(r"[^A-Za-z0-9_]+", "_", str(title or "Report"))[:31] or "Report"
        out = df.copy() if df is not None else pd.DataFrame()
        out.to_excel(writer, index=False, sheet_name=sheet)
        ws = writer.book[sheet]
        ws.freeze_panes = "A2"
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col[:200])
            ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 38)
    return buf.getvalue()


def _df_to_print_html(df, title, max_rows=600):
    rows = ""
    if df is not None and not df.empty:
        out = df.copy().head(max_rows)
        head = "".join(f"<th>{_html.escape(str(c))}</th>" for c in out.columns)
        body = ""
        for _, r in out.iterrows():
            body += "<tr>" + "".join(
                f"<td>{_html.escape(str(r.get(c, '') if r.get(c, '') is not None else ''))}</td>"
                for c in out.columns
            ) + "</tr>"
        rows = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        if len(df) > max_rows:
            rows += f"<p>Showing first {max_rows} rows of {len(df)}.</p>"
    else:
        rows = "<p>No records.</p>"
    subtitle = f"Generated on {date.today().isoformat()}"
    return _simple_doc_html(title, subtitle, rows)


def _df_to_pdf_bytes(df, title, max_rows=250):
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    buf = BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=18, rightMargin=18,
                            topMargin=18, bottomMargin=18)
    styles = getSampleStyleSheet()
    story = [Paragraph(str(title), styles["Title"]), Spacer(1, 8)]
    if df is None or df.empty:
        story.append(Paragraph("No records.", styles["Normal"]))
    else:
        out = df.copy().head(max_rows)
        cols = [str(c) for c in out.columns[:10]]
        data = [cols]
        for _, row in out.iterrows():
            data.append([
                Paragraph(_html.escape(str(row.get(c, "") if row.get(c, "") is not None else ""))[:90],
                          styles["BodyText"])
                for c in out.columns[:10]
            ])
        tbl = Table(data, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ]))
        story.append(tbl)
        if len(df) > max_rows:
            story.append(Spacer(1, 8))
            story.append(Paragraph(f"Showing first {max_rows} rows of {len(df)}.", styles["Normal"]))
    doc.build(story)
    return buf.getvalue()


def _metrics(*args):
    cols = st.columns(len(args))
    for col, (label, value) in zip(cols, args):
        col.metric(label, value)


def _ledger_mobile(account_name, is_supplier=False):
    if is_supplier:
        rows = _q("""
            SELECT COALESCE(mobile,'') AS mobile
            FROM parties
            WHERE party_name = %(p)s
            LIMIT 1
        """, {"p": account_name})
    else:
        rows = _q("""
            SELECT mobile FROM (
                SELECT COALESCE(p.mobile,'') AS mobile, 1 AS ord
                FROM parties p WHERE p.party_name = %(p)s
                UNION ALL
                SELECT COALESCE(patient_mobile,'') AS mobile, 2 AS ord
                FROM orders
                WHERE COALESCE(patient_name, party_name) = %(p)s
                UNION ALL
                SELECT COALESCE(mobile,'') AS mobile, 3 AS ord
                FROM patients
                WHERE master_name = %(p)s
            ) x
            WHERE COALESCE(mobile,'') <> ''
            ORDER BY ord
            LIMIT 1
        """, {"p": account_name})
    return rows[0]["mobile"] if rows else ""


def _ledger_email(account_name):
    rows = _q("""
        SELECT email FROM (
            SELECT COALESCE(email,'') AS email, 1 AS ord
            FROM parties WHERE party_name = %(p)s
            UNION ALL
            SELECT COALESCE(email,'') AS email, 2 AS ord
            FROM patients WHERE master_name = %(p)s
        ) x
        WHERE COALESCE(email,'') <> ''
        ORDER BY ord
        LIMIT 1
    """, {"p": account_name})
    return rows[0]["email"] if rows else ""


def _mailto_url(email, subject, body):
    email = str(email or "").strip()
    if not email or "@" not in email:
        return ""
    return (
        "mailto:"
        + urllib.parse.quote(email)
        + "?subject="
        + urllib.parse.quote(str(subject or ""))
        + "&body="
        + urllib.parse.quote(str(body or ""))
    )


def _ledger_balance_text(amount, is_supplier=False):
    amt = float(amount or 0)
    if abs(amt) < 0.01:
        return "Nil"
    if is_supplier:
        return ("Payable to supplier: " + _fmt(abs(amt))) if amt < 0 else ("Advance with supplier: " + _fmt(amt))
    return ("Balance to be received: " + _fmt(amt)) if amt > 0 else ("Credit balance: " + _fmt(abs(amt)))


def _mini_ledger_message(account_name, df, opening, closing, fd, td,
                         ledger_basis, is_supplier=False):
    lines = [
        f"Hello {account_name},",
        "",
        f"Mini ledger statement ({ledger_basis})",
        f"Period: {fd} to {td}",
        f"Opening: {_fmt(opening)}",
        "",
        "Last 5 transactions:",
    ]
    recent = df.tail(5) if df is not None and not df.empty else pd.DataFrame()
    if recent.empty:
        lines.append("- No transactions in selected period")
    else:
        for _, r in recent.iterrows():
            typ = str(r.get("Type") or "")
            ref = str(r.get("Ref No") or "")
            dt = str(r.get("Date") or "")
            dr = float(r.get("Dr (₹)") or 0)
            cr = float(r.get("Cr (₹)") or 0)
            if is_supplier:
                sign = "-" if dr > 0 else "+"
                amt = dr if dr > 0 else cr
            else:
                sign = "+" if dr > 0 else "-"
                amt = dr if dr > 0 else cr
            lines.append(f"{sign} {typ} {ref} date {dt} {_fmt(amt)}")
    lines += ["", _ledger_balance_text(closing, is_supplier)]
    return "\n".join(lines)


def _ledger_pdf_html(account_name, df, opening, closing, fd, td, ledger_basis,
                     is_supplier=False):
    rows = ""
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            rows += (
                "<tr>"
                f"<td>{_html.escape(str(r.get('Date','')))}</td>"
                f"<td>{_html.escape(str(r.get('Type','')))}</td>"
                f"<td>{_html.escape(str(r.get('Ref No','')))}</td>"
                f"<td class='r'>{_fmt(r.get('Dr (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Cr (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Balance (₹)'))}</td>"
                "</tr>"
            )
    table = (
        "<table><thead><tr><th>Date</th><th>Type</th><th>Ref</th>"
        "<th class='r'>Dr</th><th class='r'>Cr</th><th class='r'>Balance</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    totals = (
        "<div class='tot'>"
        f"<div><span>Opening</span><b>{_fmt(opening)}</b></div>"
        f"<div class='grand'><span>{_html.escape(_ledger_balance_text(closing, is_supplier))}</span><b>{_fmt(abs(closing))}</b></div>"
        "</div>"
    )
    if not is_supplier and float(closing or 0) > 0:
        shop = _shop_payment_info()
        qr = _upi_qr_html(
            shop.get("shop_upi_id", ""),
            shop.get("shop_name", "DV Optical"),
            abs(float(closing or 0)),
            f"Ledger {account_name} {fd} to {td}",
        )
        if qr:
            totals += (
                "<div class='paybox'>"
                "<div><div class='paybox-title'>Payment QR</div>"
                "<div class='paybox-sub'>For closing receivable balance "
                f"<b>{_fmt(abs(closing))}</b><br>{_html.escape(str(account_name))}</div></div>"
                f"{qr}"
                "</div>"
            )
    subtitle = f"Ledger basis: {_html.escape(str(ledger_basis))}<br>Period: {fd} to {td}"
    return _simple_doc_html(f"Ledger - {account_name}", subtitle, table, totals)


def _render_ledger_share_actions(account_name, df, opening, closing, fd, td,
                                 ledger_basis, is_supplier=False, key="ledger"):
    st.markdown("#### 📤 Send / Print Ledger")
    with st.container():
        msg = _mini_ledger_message(account_name, df, opening, closing, fd, td,
                                   ledger_basis, is_supplier=is_supplier)
        html_doc = _ledger_pdf_html(account_name, df, opening, closing, fd, td,
                                    ledger_basis, is_supplier=is_supplier)
        mobile = _ledger_mobile(account_name, is_supplier=is_supplier)
        mobile_key = f"{key}_mobile_override"
        send_mobile = _mobile_input_save_panel(key, account_name, mobile)
        mini_url = _wa_url(send_mobile, msg)
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            _open_print_html(html_doc, f"ledger_{account_name}_{fd}_{td}.html",
                             f"{key}_print_{_safe_key(account_name)}")
        with c2:
            if mini_url:
                st.link_button("📲 WA Mini Ledger", mini_url, use_container_width=True)
            else:
                st.button("📲 WA Mini Ledger", disabled=True,
                          key=f"{key}_wa_mini_dis_{_safe_key(account_name)}",
                          use_container_width=True)
                st.caption("Enter valid 10-digit WhatsApp mobile.")
        with c3:
            try:
                lan_url = _save_and_lan_url(
                    html_doc,
                    f"ledger_{account_name}_{fd}_{td}.html",
                )
                link_msg = f"{msg}\n\nView / Print\n{lan_url}"
                link_url = _wa_url(send_mobile, link_msg)
            except Exception as exc:
                link_url = ""
                lan_url = ""
                st.caption(f"Ledger print-link unavailable: {exc}")
            if link_url:
                st.link_button("📲 WA + Ledger Link", link_url, use_container_width=True)
            else:
                st.button("📲 WA + Ledger Link", disabled=True,
                          key=f"{key}_wa_link_dis_{_safe_key(account_name)}",
                          use_container_width=True)
        with c4:
            email = st.text_input(
                "Email",
                value=_ledger_email(account_name),
                key=f"{key}_email_{_safe_key(account_name)}",
                placeholder="party@example.com",
                label_visibility="collapsed",
            )
            try:
                mail_lan_url = lan_url or _save_and_lan_url(
                    html_doc,
                    f"ledger_{account_name}_{fd}_{td}.html",
                )
            except Exception:
                mail_lan_url = ""
            subject = f"Ledger statement - {account_name} - {fd} to {td}"
            body = f"{msg}\n\nView / Print\n{mail_lan_url}" if mail_lan_url else msg
            mail_url = _mailto_url(email, subject, body)
            if mail_url:
                st.link_button("✉️ Email Ledger", mail_url, use_container_width=True)
            else:
                st.button(
                    "✉️ Email Ledger",
                    disabled=True,
                    key=f"{key}_email_dis_{_safe_key(account_name)}",
                    use_container_width=True,
                    help="Enter a valid email address.",
                )
        st.text_area("Mini ledger message", msg, height=190,
                     key=f"{key}_mini_text_{_safe_key(account_name)}")
        with st.popover("WhatsApp URL / copy", use_container_width=True):
            st.text_input("Mini ledger WhatsApp URL", mini_url or "",
                          key=f"{key}_mini_url_{_safe_key(account_name)}")
            st.text_area("Mini ledger message copy", msg, height=190,
                         key=f"{key}_mini_copy_{_safe_key(account_name)}")


def _party_product_ledger_rows(account_name, fd, td):
    return _q("""
        SELECT
            i.invoice_date::text AS "Date",
            i.invoice_no         AS "Invoice No",
            COALESCE(p.main_group,'') AS "Group",
            COALESCE(p.brand,'') AS "Brand",
            COALESCE(p.product_name, ol.lens_params->>'product_name', '') AS "Product",
            COALESCE(p.coating, ol.lens_params->>'coating', '') AS "Coating",
            COALESCE(p.index_value::text, ol.lens_params->>'lens_index', '') AS "Index",
            COALESCE(ol.eye_side,'') AS "Eye",
            TRIM(BOTH ' ' FROM CONCAT_WS(' ',
                CASE WHEN ol.sph IS NOT NULL THEN 'SPH ' || to_char(ol.sph, 'FM+999990.00') END,
                CASE WHEN ol.cyl IS NOT NULL AND ABS(ol.cyl) > 0.001 THEN 'CYL ' || to_char(ol.cyl, 'FM+999990.00') END,
                CASE WHEN ol.axis IS NOT NULL AND ol.axis <> 0 THEN 'AX ' || ol.axis::text END,
                CASE WHEN ol.add_power IS NOT NULL AND ol.add_power <> 0 THEN 'ADD ' || to_char(ol.add_power, 'FM+999990.00') END
            )) AS "Power",
            COALESCE(p.unit, ol.lens_params->>'unit', '') AS "Unit",
            ROUND(COALESCE(ol.quantity, ol.billing_qty, 0), 2) AS "Qty",
            ROUND(COALESCE(ol.unit_price,0), 2) AS "Rate (₹)",
            ROUND(COALESCE(ol.unit_price,0) * COALESCE(ol.quantity, ol.billing_qty, 0), 2) AS "Gross (₹)",
            ROUND(COALESCE(ol.discount_amount,0), 2) AS "Discount (₹)",
            ROUND(GREATEST(0, COALESCE(ol.total_price, ol.billing_total, ol.unit_price * COALESCE(ol.quantity,1), 0) - COALESCE(ol.gst_amount,0)), 2) AS "Taxable (₹)",
            ROUND(COALESCE(ol.gst_percent,0), 2) AS "GST %%",
            ROUND(COALESCE(ol.gst_amount,0), 2) AS "GST (₹)",
            ROUND(COALESCE(ol.total_price, ol.billing_total, ol.unit_price * COALESCE(ol.quantity,1), 0), 2) AS "Total (₹)"
        FROM invoices i
        LEFT JOIN parties pt ON pt.id = i.party_id
        JOIN order_lines ol ON ol.order_id::text = ANY(i.order_ids::text[])
        JOIN orders o ON o.id = ol.order_id
        LEFT JOIN products p ON p.id = ol.product_id
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted,FALSE)=FALSE
          AND UPPER(COALESCE(i.status,'')) NOT IN ('VOID','CANCELLED')
          AND COALESCE(ol.is_deleted,FALSE)=FALSE
          AND COALESCE(ol.is_service_line,FALSE)=FALSE
          AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') = %(party)s
        ORDER BY i.invoice_date, i.invoice_no, "Product", "Eye"
    """, {"party": account_name, "fd": fd, "td": td})


def _party_invoice_detail_ledger_rows(account_name, fd, td):
    return _q(f"""
        WITH entries AS (
            SELECT
                i.invoice_date::date AS entry_date,
                i.created_at AS sort_time,
                i.invoice_no::text AS doc_no,
                'INVOICE'::text AS entry_type,
                COALESCE(p.product_name, ol.lens_params->>'product_name', '') AS product,
                TRIM(BOTH ' | ' FROM CONCAT_WS(' | ',
                    NULLIF(COALESCE(p.brand,''), ''),
                    NULLIF(COALESCE(p.coating, ol.lens_params->>'coating', ''), ''),
                    NULLIF(COALESCE(p.index_value::text, ol.lens_params->>'lens_index', ''), '')
                )) AS product_specs,
                COALESCE(ol.eye_side,'') AS eye,
                TRIM(BOTH ' ' FROM CONCAT_WS(' ',
                    CASE WHEN ol.sph IS NOT NULL THEN 'SPH ' || to_char(ol.sph, 'FM+999990.00') END,
                    CASE WHEN ol.cyl IS NOT NULL AND ABS(ol.cyl) > 0.001 THEN 'CYL ' || to_char(ol.cyl, 'FM+999990.00') END,
                    CASE WHEN ol.axis IS NOT NULL AND ol.axis <> 0 THEN 'AX ' || ol.axis::text END,
                    CASE WHEN ol.add_power IS NOT NULL AND ol.add_power <> 0 THEN 'ADD ' || to_char(ol.add_power, 'FM+999990.00') END
                )) AS power,
                COALESCE(p.unit, ol.lens_params->>'unit', '') AS unit,
                COALESCE(ol.quantity, ol.billing_qty, 0)::numeric AS qty,
                COALESCE(ol.unit_price,0)::numeric AS rate,
                COALESCE(ol.discount_amount,0)::numeric AS discount,
                COALESCE(ol.gst_amount,0)::numeric AS gst,
                COALESCE(ol.total_price, ol.billing_total, ol.unit_price * COALESCE(ol.quantity,1), 0)::numeric AS debit,
                0::numeric AS credit,
                ('Invoice ' || COALESCE(i.invoice_no,'')) AS narration
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            JOIN order_lines ol ON ol.order_id::text = ANY(i.order_ids::text[])
            JOIN orders o ON o.id = ol.order_id
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
              AND COALESCE(i.is_deleted,FALSE)=FALSE
              AND UPPER(COALESCE(i.status,'')) NOT IN ('VOID','CANCELLED')
              AND COALESCE(ol.is_deleted,FALSE)=FALSE
              AND COALESCE(ol.is_service_line,FALSE)=FALSE
              AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') = %(party)s

            UNION ALL

            SELECT
                p.payment_date::date AS entry_date,
                p.created_at AS sort_time,
                COALESCE(NULLIF(p.payment_no,''), p.id::text) AS doc_no,
                COALESCE(NULLIF(p.payment_type,''), 'PAYMENT')::text AS entry_type,
                ''::text AS product,
                COALESCE(NULLIF(p.payment_mode,''), p.method, '') AS product_specs,
                ''::text AS eye,
                ''::text AS power,
                ''::text AS unit,
                0::numeric AS qty,
                0::numeric AS rate,
                0::numeric AS discount,
                0::numeric AS gst,
                0::numeric AS debit,
                COALESCE(p.amount,0)::numeric AS credit,
                {_PAY_NARRATION_EXPR} AS narration
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            CROSS JOIN LATERAL (SELECT {_PAY_PARTY_EXPR} AS party_name) party_x
            WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
              AND p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
              AND NOT COALESCE(p.is_deleted,FALSE)
              AND party_x.party_name = %(party)s
        )
        SELECT
            entry_date::text AS "Date",
            doc_no AS "Invoice / Receipt",
            entry_type AS "Entry",
            product AS "Product",
            product_specs AS "Index / Coating / Mode",
            eye AS "Eye",
            power AS "Power",
            unit AS "Unit",
            ROUND(qty, 2) AS "Qty",
            ROUND(rate, 2) AS "Rate (₹)",
            ROUND(discount, 2) AS "Discount (₹)",
            ROUND(gst, 2) AS "GST (₹)",
            ROUND(debit, 2) AS "Debit (₹)",
            ROUND(credit, 2) AS "Credit (₹)",
            narration AS "Narration"
        FROM entries
        ORDER BY entry_date, sort_time, doc_no, entry_type DESC, product, eye
    """, {"party": account_name, "fd": fd, "td": td})


def _invoice_detail_ledger_html(account_name, df, opening, closing, fd, td):
    rows_html = ""
    current_doc = None
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            doc = str(r.get("Invoice / Receipt") or "")
            entry = str(r.get("Entry") or "")
            if doc != current_doc:
                current_doc = doc
                rows_html += (
                    "<tr class='docrow'>"
                    f"<td colspan='11'>{_html.escape(str(r.get('Date') or ''))} &nbsp; "
                    f"<b>{_html.escape(doc)}</b> &nbsp; {_html.escape(entry)}</td>"
                    "</tr>"
                )
            rows_html += (
                "<tr>"
                f"<td>{_html.escape(entry)}</td>"
                f"<td>{_html.escape(str(r.get('Product') or ''))}</td>"
                f"<td>{_html.escape(str(r.get('Index / Coating / Mode') or ''))}</td>"
                f"<td>{_html.escape(str(r.get('Eye') or ''))}</td>"
                f"<td>{_html.escape(str(r.get('Power') or ''))}</td>"
                f"<td class='r'>{float(r.get('Qty') or 0):g}</td>"
                f"<td class='r'>{_fmt(r.get('Rate (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Discount (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Debit (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Credit (₹)'))}</td>"
                f"<td class='r'>{_fmt(r.get('Balance (₹)'))}</td>"
                "</tr>"
            )
    table = (
        "<style>.docrow td{background:#e5e7eb!important;font-weight:900;color:#111}</style>"
        "<table><thead><tr><th>Entry</th><th>Product</th><th>Index / Coating / Mode</th>"
        "<th>Eye</th><th>Power</th><th class='r'>Qty</th><th class='r'>Rate</th>"
        "<th class='r'>Disc</th><th class='r'>Debit</th><th class='r'>Credit</th>"
        "<th class='r'>Balance</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )
    totals = (
        "<div class='tot'>"
        f"<div><span>Opening</span><b>{_fmt(opening)}</b></div>"
        f"<div><span>Closing</span><b>{_fmt(closing)}</b></div>"
        "</div>"
    )
    subtitle = f"Invoice detail ledger<br>Period: {fd} to {td}"
    return _simple_doc_html(f"Invoice Detail Ledger - {account_name}", subtitle, table, totals)


def _render_party_ledger_special_reports(account_name, fd, td, key="plprod"):
    st.markdown("#### Party Report Views")
    rows = _party_product_ledger_rows(account_name, fd, td)
    if not rows:
        st.info("No invoiced product lines found for this party in selected period.")
        return
    base = _df(rows)
    for c in ["Qty", "Rate (₹)", "Gross (₹)", "Discount (₹)", "Taxable (₹)", "GST %", "GST (₹)", "Total (₹)"]:
        if c in base.columns:
            base[c] = pd.to_numeric(base[c], errors="coerce").fillna(0)

    tab_invoice, tab_date, tab_product, tab_power = st.tabs([
        "Invoice Detail Ledger",
        "Product Date Wise",
        "Product + Coating + Index",
        "Power Wise Detail",
    ])
    with tab_invoice:
        inv_rows = _party_invoice_detail_ledger_rows(account_name, fd, td)
        if not inv_rows:
            st.info("No invoice/payment detail found for this party in selected period.")
        else:
            inv_df = _df(inv_rows)
            for c in ["Qty", "Rate (₹)", "Discount (₹)", "GST (₹)", "Debit (₹)", "Credit (₹)"]:
                if c in inv_df.columns:
                    inv_df[c] = pd.to_numeric(inv_df[c], errors="coerce").fillna(0)
            inv_df["Balance (₹)"] = (inv_df["Debit (₹)"] - inv_df["Credit (₹)"]).cumsum()
            closing = float(inv_df["Balance (₹)"].iloc[-1]) if not inv_df.empty else 0.0
            st.dataframe(inv_df, use_container_width=True, hide_index=True)
            html_doc = _invoice_detail_ledger_html(account_name, inv_df, 0, closing, fd, td)
            b1, b2 = st.columns(2)
            with b1:
                _direct_print_html(
                    html_doc,
                    f"Invoice_Detail_Ledger_{account_name}_{fd}_{td}.html",
                    f"{key}_invoice_direct_full",
                )
            with b2:
                _open_print_html(
                    html_doc,
                    f"Invoice_Detail_Ledger_{account_name}_{fd}_{td}.html",
                    f"{key}_invoice_browser_full",
                )
            _export(inv_df, f"Invoice_Detail_Ledger_{account_name}_{fd}_{td}", f"{key}_invoice")

    with tab_date:
        st.caption("Invoice-date-wise product calculation for party sharing.")
        st.dataframe(base, use_container_width=True, hide_index=True)
        _export(base, f"Product_Date_Wise_{account_name}_{fd}_{td}", f"{key}_date")

    with tab_product:
        group_cols = ["Product", "Brand", "Coating", "Index", "Unit"]
        grouped = (
            base.groupby(group_cols, dropna=False, as_index=False)
            .agg({
                "Qty": "sum",
                "Gross (₹)": "sum",
                "Discount (₹)": "sum",
                "Taxable (₹)": "sum",
                "GST (₹)": "sum",
                "Total (₹)": "sum",
            })
            .sort_values("Total (₹)", ascending=False)
        )
        st.dataframe(grouped, use_container_width=True, hide_index=True)
        _export(grouped, f"Product_Coating_Index_{account_name}_{fd}_{td}", f"{key}_prod")

    with tab_power:
        power_cols = ["Product", "Brand", "Coating", "Index", "Power", "Eye", "Unit"]
        pwr = (
            base.groupby(power_cols, dropna=False, as_index=False)
            .agg({
                "Qty": "sum",
                "Gross (₹)": "sum",
                "Discount (₹)": "sum",
                "Taxable (₹)": "sum",
                "GST (₹)": "sum",
                "Total (₹)": "sum",
            })
            .sort_values(["Product", "Power", "Eye"])
        )
        st.dataframe(pwr, use_container_width=True, hide_index=True)
        _export(pwr, f"Product_Power_Wise_{account_name}_{fd}_{td}", f"{key}_power")


_PAY_PARTY_EXPR = """
COALESCE(NULLIF(p.party_name,''), ip.party_name, cp.party_name,
         (SELECT COALESCE(NULLIF(o.party_name,''), NULLIF(o.patient_name,''))
          FROM orders o
          WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
             OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
             OR (i.order_ids IS NOT NULL AND o.id::text = ANY(i.order_ids::text[]))
             OR (c.order_ids IS NOT NULL AND o.id::text = ANY(c.order_ids::text[]))
          ORDER BY o.created_at DESC LIMIT 1), '')
"""

_PAY_NARRATION_EXPR = """
COALESCE(NULLIF(p.remarks,''),
         CASE
             WHEN COALESCE(p.is_advance,FALSE) OR p.payment_type = 'ADVANCE'
                 THEN 'Advance at order punching'
             WHEN p.invoice_id IS NOT NULL
                 THEN 'Payment against invoice ' || COALESCE(i.invoice_no, '')
             WHEN p.challan_id IS NOT NULL
                 THEN 'Payment against challan ' || COALESCE(c.challan_no, '')
             WHEN p.payment_type = 'DISBURSEMENT'
                 THEN 'Payment disbursement'
             ELSE COALESCE(p.payment_type, 'Payment')
         END)
"""


def _live_party_filter_expr(alias="x"):
    return f"AND {alias}.party_name ILIKE %(pty)s"


def _account_scope_clause(key, order_alias="o", invoice_alias="i"):
    scope = st.session_state.get(f"{key}_scope", "All")
    order_type = f"UPPER(COALESCE({order_alias}.order_type,''))"
    if scope == "Retail":
        return f"AND {order_type} IN ('RETAIL','CONSULTATION')"
    if scope == "Online":
        return f"AND {order_type} = 'ONLINE'"
    if scope == "Wholesale":
        return (
            f"AND (COALESCE({invoice_alias}.party_id::text,'') <> '' "
            f"OR {order_type} IN ('WHOLESALE','BULK','BULK_ORDER')) "
            f"AND {order_type} NOT IN ('RETAIL','CONSULTATION','ONLINE')"
        )
    return ""


def _payment_scope_clause(key):
    scope = st.session_state.get(f"{key}_scope", "All")
    order_match = """
        (
            (p.order_id IS NOT NULL AND ox.id = p.order_id)
         OR (p.advance_for_order_id IS NOT NULL AND ox.id = p.advance_for_order_id)
         OR (i.order_ids IS NOT NULL AND ox.id::text = ANY(i.order_ids::text[]))
         OR (c.order_ids IS NOT NULL AND ox.id::text = ANY(c.order_ids::text[]))
        )
    """
    if scope == "Retail":
        return f"AND EXISTS (SELECT 1 FROM orders ox WHERE {order_match} AND UPPER(COALESCE(ox.order_type,'')) IN ('RETAIL','CONSULTATION'))"
    if scope == "Online":
        return f"AND EXISTS (SELECT 1 FROM orders ox WHERE {order_match} AND UPPER(COALESCE(ox.order_type,'')) = 'ONLINE')"
    if scope == "Wholesale":
        return (
            f"AND NOT EXISTS (SELECT 1 FROM orders ox WHERE {order_match} "
            f"AND UPPER(COALESCE(ox.order_type,'')) IN ('RETAIL','CONSULTATION','ONLINE'))"
        )
    return ""


def _apply_grouping(df, grouping, dr_col, cr_col, date_col="Date"):
    """Collapse detail rows into daily/monthly/yearly summary."""
    if grouping == "Detail" or date_col not in df.columns:
        return df

    df = df.copy()
    try:
        df["_dt"] = pd.to_datetime(df[date_col], errors="coerce")
        if grouping == "Daily":
            df["Period"] = df["_dt"].dt.strftime("%Y-%m-%d")
        elif grouping == "Monthly":
            df["Period"] = df["_dt"].dt.strftime("%Y-%m")
        else:
            df["Period"] = df["_dt"].dt.year.astype(str)

        agg = {"Entries": (dr_col, "count")}
        if dr_col in df.columns: agg["Dr (₹)"] = (dr_col, "sum")
        if cr_col in df.columns: agg["Cr (₹)"] = (cr_col, "sum")
        grp = df.groupby("Period").agg(**agg).reset_index()
        if "Dr (₹)" in grp and "Cr (₹)" in grp:
            grp["Net (₹)"] = grp["Dr (₹)"] - grp["Cr (₹)"]
        return grp.sort_values("Period")
    except Exception:
        return df


# ══════════════════════════════════════════════════════════════════════════════
# 1. SALES REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_sales_register():
    st.caption("All invoices raised -- line-wise with GST breakup")
    fd, td   = _date_filter("sr", default_preset="This year")
    party    = _party_filter("sr", "All Parties / Customers", include_patients=True)
    grouping = _grouping("sr")

    pf  = "AND COALESCE(NULLIF(p.party_name,''), NULLIF(o.party_name,''), NULLIF(o.patient_name,''), '') ILIKE %(pty)s" if party else ""
    scope_filter = _account_scope_clause("sr", "o", "i")
    rows = _q("""
        SELECT
            i.invoice_date::text        AS "Date",
            i.invoice_no                AS "Invoice No",
            COALESCE(NULLIF(p.party_name,''), NULLIF(o.party_name,''), NULLIF(o.patient_name,''), '')  AS "Party",
            COALESCE(p.city,'')         AS "City",
            COALESCE(p.gstin,'')        AS "GSTIN",
            ROUND(i.total_amount, 2)    AS "Taxable (₹)",
            ROUND(i.total_tax/2, 2)     AS "CGST (₹)",
            ROUND(i.total_tax/2, 2)     AS "SGST (₹)",
            ROUND(i.total_tax, 2)       AS "Total Tax (₹)",
            ROUND(i.grand_total, 2)     AS "Invoice Amt (₹)",
            -- Use allocator-maintained fields -- no manual advance recalculation
            ROUND(COALESCE(i.amount_paid, 0), 2)                  AS "Paid (₹)",
            ROUND(COALESCE(i.balance_due, i.grand_total), 2)      AS "Balance (₹)",
            COALESCE(i.payment_status, 'UNPAID')                   AS "Status"
        FROM invoices i
        LEFT JOIN parties p ON p.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2
            WHERE o2.id::text = ANY(i.order_ids)
            LIMIT 1
        ) o ON TRUE
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
          """+ (pf or "") + """ """+ scope_filter + """
        ORDER BY i.invoice_date DESC, i.invoice_no
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%"})

    if not rows:
        st.info("No invoices in this period."); return

    df = _df(rows)
    for c in ["Taxable (₹)","CGST (₹)","SGST (₹)","Total Tax (₹)","Invoice Amt (₹)","Paid (₹)","Balance (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    # Do NOT recalculate Balance from Paid -- use DB balance_due (CN-adjusted by allocator)
    df["Excess (₹)"] = (df["Paid (₹)"] - df["Invoice Amt (₹)"]).clip(lower=0)
    if "Status" in df.columns:
        df.loc[df["Excess (₹)"] > 0.50, "Status"] = "EXCESS"

    _metrics(
        ("Invoices",    str(len(df))),
        ("Taxable",     _fmt(df["Taxable (₹)"].sum())),
        ("Total Tax",   _fmt(df["Total Tax (₹)"].sum())),
        ("Invoice Amt", _fmt(df["Invoice Amt (₹)"].sum())),
        ("Collected",   _fmt(df["Paid (₹)"].sum())),
        ("Outstanding", _fmt(df["Balance (₹)"].sum())),
        ("Excess",      _fmt(df["Excess (₹)"].sum())),
    )

    display = _apply_grouping(df, grouping, "Invoice Amt (₹)", "Paid (₹)")
    _render_register_grid(
        display,
        "sr",
        select_col="Invoice No",
        select_state_key="sr_doc_sel",
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns},
    )
    _sales_invoice_action_drawer(df, "sr_doc")
    _export(df, f"Sales_Register_{fd}_{td}", "sr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 2. PURCHASE REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_purchase_register():
    st.caption("All purchase invoices -- from procurement")
    fd, td   = _date_filter("pr")
    party    = _party_filter("pr", "All Suppliers")
    grouping = _grouping("pr")

    # Fix: also match on pi.supplier_name so multi-challan invoices where
    # supplier_id is NULL (LEFT JOIN gives NULL s.party_name) are not dropped.
    pf = "AND (s.party_name ILIKE %(pty)s OR pi.supplier_name ILIKE %(pty)s)" if party else ""

    # Try purchase_invoices table first; fallback to disbursement payments
    rows = []
    try:
        rows = _q("""
            SELECT
                pi.invoice_date::text           AS "Date",
                pi.invoice_no                   AS "Invoice No",
                COALESCE(s.party_name,
                         pi.supplier_name, '')  AS "Supplier",
                ROUND(pi.subtotal, 2)           AS "Taxable (₹)",
                ROUND(pi.gst_amount, 2)         AS "Tax (₹)",
                ROUND(pi.invoice_total, 2)      AS "Total (₹)",
                pi.payment_status               AS "Status"
            FROM purchase_invoices pi
            LEFT JOIN parties s ON s.id::text = pi.supplier_id
            WHERE (pi.invoice_date BETWEEN %(fd)s AND %(td)s
                   OR pi.created_at::date BETWEEN %(fd)s AND %(td)s)
              AND COALESCE(pi.is_deleted, FALSE) = FALSE
              AND COALESCE(pi.payment_status,'') != 'VOIDED'
              """+ (pf or "") + """
            ORDER BY COALESCE(pi.created_at::date, pi.invoice_date) DESC, pi.invoice_date DESC
        """, {"fd": fd, "td": td, "pty": f"%{party or ''}%"})
    except Exception:
        rows = []  # table doesn't exist yet -- use fallback below

    if not rows:
        pay_pf = "AND p.party_name ILIKE %(pty)s" if party else ""
        rows = _q("""
            SELECT
                p.payment_date::text     AS "Date",
                p.payment_no             AS "Invoice No",
                COALESCE(p.party_name,'') AS "Supplier",
                0                        AS "Taxable (₹)",
                0                        AS "Tax (₹)",
                ROUND(p.amount,2)        AS "Total (₹)",
                'PAID'                   AS "Status"
            FROM payments p
            WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
              AND p.payment_type = 'DISBURSEMENT'
              AND COALESCE(p.is_deleted,FALSE) = FALSE
              """ + pay_pf + """
            ORDER BY p.payment_date DESC
        """, {"fd": fd, "td": td, "pty": f"%{party or ''}%"})
        if rows:
            st.caption("ℹ️ Showing disbursements (purchase_invoices table not found)")

    if not rows:
        # ── No formal invoices yet -- show Post to Accounts panel ─────────
        st.info("No purchase invoices in this period.")

        # Offer to post any unposted PA records as formal purchase invoices
        st.markdown("---")
        st.markdown(
            "<div style='background:#0a1628;border:1px solid #1e3a5f;"
            "border-left:4px solid #f59e0b;border-radius:8px;"
            "padding:10px 14px;margin-bottom:10px'>"
            "<b style='color:#fbbf24'>📤 Post Procurement to Accounts</b>"
            "<div style='color:#94a3b8;font-size:0.78rem;margin-top:4px'>"
            "Order has been procured but not yet posted as a formal purchase invoice. "
            "Enter an order number to create the purchase invoice record.</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        _pc1, _pc2 = st.columns([4, 1])
        _post_ono = _pc1.text_input(
            "Order No to post",
            placeholder="e.g. R/2627/0121",
            key="pr_post_ono",
            label_visibility="collapsed",
        )
        _do_post = _pc2.button("📤 Post", key="pr_post_btn",
                               type="primary", use_container_width=True)
        if _do_post and _post_ono.strip():
            try:
                from modules.procurement.purchase_invoice import (
                    convert_acknowledgement_to_invoice
                )
                _result = convert_acknowledgement_to_invoice(_post_ono.strip())
                if _result["ok"]:
                    st.success(_result["message"])
                    st.rerun()
                else:
                    st.error(_result["message"])
            except Exception as _pe:
                st.error(f"Post failed: {_pe}")
        return

    df = _df(rows)
    for c in ["Taxable (₹)","Tax (₹)","Total (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Entries",   str(len(df))),
        ("Taxable",   _fmt(df["Taxable (₹)"].sum()) if "Taxable (₹)" in df.columns else "--"),
        ("Tax",       _fmt(df["Tax (₹)"].sum()) if "Tax (₹)" in df.columns else "--"),
        ("Total",     _fmt(df["Total (₹)"].sum())),
    )
    # Fix: dr_col=Total, cr_col=Tax so grouped "Net" = Total spend, not GST-only.
    display = _apply_grouping(df, grouping, "Total (₹)", "Tax (₹)")
    _render_register_grid(
        display,
        "pr",
        select_col="Invoice No",
        select_state_key="pr_doc_sel",
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns},
    )
    _purchase_invoice_action_drawer(df, "pr_doc")
    _export(df, f"Purchase_Register_{fd}_{td}", "pr_dl")

    # ── Per-invoice void / cancel ─────────────────────────────────────────────
    # Allows cancelling a specific invoice directly from Registers.
    # This resets all linked PA rows so they reappear in the Purchase Register
    # module's challan picker for re-posting.
    with st.expander("🗑️ Void / Cancel an Invoice", expanded=False):
        st.caption(
            "Select an invoice to void. All its lines return to un-posted challan state "
            "and can be re-posted. Paid invoices cannot be voided here."
        )
        _unpaid = df[df["Status"] != "PAID"]["Invoice No"].tolist() if "Status" in df.columns else df["Invoice No"].tolist()
        if not _unpaid:
            st.info("All invoices in this period are already paid and cannot be voided here.")
        else:
            _void_sel = st.selectbox("Select invoice to void", _unpaid, key="reg_void_sel")
            _void_confirm = st.checkbox(
                f"I confirm I want to void invoice **{_void_sel}** and return all its lines to un-posted state.",
                key="reg_void_confirm"
            )
            if st.button("🗑️ Void Invoice", key="reg_void_btn",
                         type="primary", disabled=not _void_confirm):
                try:
                    from modules.sql_adapter import run_write
                    run_write("""
                        UPDATE purchase_acknowledgements
                        SET billing_status = 'PURCHASE_ACKED',
                            invoice_no     = NULL,
                            document_date  = CASE
                                WHEN document_date > CURRENT_DATE THEN CURRENT_DATE
                                ELSE document_date
                            END,
                            notes = REGEXP_REPLACE(COALESCE(notes,''),
                                    'invoice:[^|]+\\|?\\s*', '', 'g')
                        WHERE billing_status = 'INVOICED'
                          AND (COALESCE(notes,'') LIKE %(ref)s OR invoice_no = %(inv)s)
                    """, {"inv": _void_sel, "ref": f"%invoice:{_void_sel}%"})
                    run_write("DELETE FROM purchase_invoice_lines WHERE invoice_no=%(inv)s",
                              {"inv": _void_sel})
                    run_write("""
                        UPDATE purchase_invoices SET
                            payment_status='VOIDED', total_items=0,
                            total_qty_received=0, subtotal=0,
                            gst_amount=0, invoice_total=0,
                            notes=COALESCE(notes,'')||' [VOIDED]', updated_at=NOW()
                        WHERE invoice_no=%(inv)s
                    """, {"inv": _void_sel})
                    st.success(
                        f"✅ Invoice {_void_sel} voided. Go to Purchase Register module "
                        f"-> challan picker to re-post the lines."
                    )
                    try:
                        st.cache_data.clear()
                    except Exception:
                        pass
                    st.session_state.pop("reg_void_confirm", None)
                    st.rerun()
                except Exception as _ve:
                    st.error(f"Void failed: {_ve}")

    # ── Correction guide ──────────────────────────────────────────────────────
    with st.expander("How to correct a wrong invoice", expanded=False):
        st.markdown(
            "**Wrong order sent to supplier / wrong lines posted? Follow these steps:**\n\n"
            "1. **Void the invoice** -- use the \"Void / Cancel an Invoice\" panel above, "
            "or open Purchase Register module -> GRN section -> Detail/Audit -> "
            "\"Void Entire Invoice\". All lines return to un-posted challan state immediately.\n\n"
            "2. **Change the order link** *(if the lines belong to a different order)* -- "
            "open Purchase Register module -> find the challan line -> Edit -> "
            "\"Re-link to a Different Order\" -> enter the correct order number -> Re-link.\n\n"
            "3. **Re-post** -- in Purchase Register module, tick the challan(s) in the "
            "\"Post Challans to Invoice\" picker -> enter the correct invoice number -> Post.\n\n"
            "4. **Verify** -- check this Registers screen; the new invoice should appear and "
            "the old voided one is gone."
        )

    # ── Also offer Post to Accounts for any new orders not yet in register ──
    with st.expander("📤 Post another order to Accounts", expanded=False):
        st.caption(
            "Enter an order number to post its procurement as a formal "
            "purchase invoice (only orders not yet posted)."
        )
        _ep1, _ep2 = st.columns([4, 1])
        _extra_ono = _ep1.text_input(
            "Order No",
            placeholder="e.g. R/2627/0122",
            key="pr_extra_ono",
            label_visibility="collapsed",
        )
        if _ep2.button("📤 Post", key="pr_extra_btn", type="primary",
                       use_container_width=True) and _extra_ono.strip():
            try:
                from modules.procurement.purchase_invoice import (
                    convert_acknowledgement_to_invoice
                )
                _r2 = convert_acknowledgement_to_invoice(_extra_ono.strip())
                if _r2["ok"]:
                    st.success(_r2["message"])
                    st.rerun()
                else:
                    st.error(_r2["message"])
            except Exception as _pe2:
                st.error(f"Post failed: {_pe2}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. PAYMENT RECEIPT BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_payment_receipt_book():
    st.caption("All money received -- party-wise, mode-wise")
    fd, td   = _date_filter("prb")
    party    = _party_filter("prb", "All Parties", include_patients=True)
    grouping = _grouping("prb")
    mode_opts = ["All Modes","CASH","UPI","NEFT","CHEQUE","RTGS","CARD","OTHER"]
    mode     = st.selectbox("Payment Mode", mode_opts, key="prb_mode")

    pf  = "AND party_x.party_name ILIKE %(pty)s" if party else ""
    mf  = "" if mode == "All Modes" else "AND UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) = %(mode)s"
    scope_filter = _payment_scope_clause("prb")

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            COALESCE(NULLIF(p.payment_no,''), p.id::text) AS "Receipt No",
            party_x.party_name           AS "Party",
            UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) AS "Mode",
            COALESCE(p.reference_no,'')  AS "Ref / UTR",
            ROUND(p.amount, 2)           AS "Amount (₹)",
            COALESCE(i.invoice_no,'--')   AS "Against Invoice",
            COALESCE(c.challan_no,'--')   AS "Against Challan",
            {_PAY_NARRATION_EXPR}        AS "Narration"
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN challans c ON c.id = p.challan_id
        LEFT JOIN parties ip ON ip.id = i.party_id
        LEFT JOIN parties cp ON cp.id = c.party_id
        CROSS JOIN LATERAL (
            SELECT {_PAY_PARTY_EXPR} AS party_name
        ) party_x
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(NULLIF(p.payment_type,''), 'PAYMENT') IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
          AND NOT COALESCE(p.is_deleted,FALSE)
          """+ (pf or "") + """ """+ (mf or "") + """ """+ scope_filter + """
        ORDER BY p.payment_date DESC, p.payment_no
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%", "mode": mode})

    if not rows:
        st.info("No receipts in this period."); return

    df = _df(rows)
    df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    # Mode breakdown
    if "Mode" in df.columns:
        mode_sum = df.groupby("Mode")["Amount (₹)"].sum()
        cols = st.columns(min(len(mode_sum), 5))
        for i, (m, v) in enumerate(mode_sum.items()):
            cols[i % len(cols)].metric(m, _fmt(v))
        st.markdown("---")

    _metrics(
        ("Receipts",      str(len(df))),
        ("Total Received",_fmt(df["Amount (₹)"].sum())),
    )
    display = _apply_grouping(df, grouping, "Amount (₹)", "Amount (₹)")
    _render_register_grid(
        display,
        "prb",
        select_col="Receipt No",
        select_state_key="prb_doc_sel",
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")},
    )
    _receipt_action_drawer(df, "prb_doc")
    _export(df, f"Receipt_Book_{fd}_{td}", "prb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 4. PAYMENT DISBURSEMENT BOOK
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# COMMON PAYMENT ACTION DRAWER  (Cash Book, Bank Book, Disbursement)
# ══════════════════════════════════════════════════════════════════════════════

def _payment_action_drawer(df, ref_col, key, book_label="Cash"):
    """Generic action drawer for Cash Book, Bank Book, Disbursement Book.

    ref_col  -- column name that holds the payment_no / voucher ref.
    book_label -- used in expander title only.
    """
    if df is None or df.empty or ref_col not in df.columns:
        return

    with st.expander(f"🔎 Open {book_label} Document / Print / WhatsApp", expanded=False):
        ref_nos = [str(x) for x in df[ref_col].dropna().tolist() if str(x).strip()]
        if not ref_nos:
            st.info("No reference numbers in this period.")
            return

        sel_key = f"{key}_sel"
        if st.session_state.get(sel_key) not in ref_nos:
            st.session_state[sel_key] = ref_nos[0]
        ref_no = st.selectbox("Select document", ref_nos, key=sel_key)
        if not ref_no:
            return

        # Fetch full payment record from DB
        rows = _q("""
            SELECT
                COALESCE(NULLIF(p.payment_no,''), p.id::text) AS ref_no,
                p.payment_date::text,
                p.payment_type,
                COALESCE(NULLIF(p.payment_mode,''), p.method, '') AS mode,
                COALESCE(p.reference_no,'') AS reference_no,
                COALESCE(p.remarks,'') AS remarks,
                ROUND(p.amount, 2) AS amount,
                COALESCE(p.party_name, ip.party_name, cp.party_name,
                    (SELECT COALESCE(NULLIF(o.party_name,''), NULLIF(o.patient_name,''))
                     FROM orders o
                     WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
                        OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
                     LIMIT 1), '') AS party_name,
                COALESCE(ip.mobile, cp.mobile,
                    (SELECT o.patient_mobile FROM orders o
                     WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
                        OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
                     LIMIT 1), '') AS mobile,
                COALESCE(i.invoice_no,'') AS invoice_no,
                COALESCE(c.challan_no,'')  AS challan_no
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            WHERE COALESCE(NULLIF(p.payment_no,''), p.id::text) = %(rno)s
            LIMIT 1
        """, {"rno": ref_no})

        if not rows:
            st.warning("Document not found.")
            return

        rec = rows[0]
        ptype  = str(rec.get("payment_type") or "PAYMENT").upper()
        mode   = str(rec.get("mode") or "").upper()
        party  = rec.get("party_name") or "—"
        amount = rec.get("amount") or 0
        dt     = rec.get("payment_date") or ""
        ref    = rec.get("reference_no") or ""
        narr   = rec.get("remarks") or ""
        inv_no = rec.get("invoice_no") or ""
        chal_no= rec.get("challan_no") or ""

        c1, c2, c3 = st.columns(3)
        c1.metric("Party / Payee", party)
        c2.metric("Amount", _fmt(amount))
        c3.metric("Mode", mode or ptype)

        # ── Build print HTML ──────────────────────────────────────────────────
        is_disbursement = ptype in ("DISBURSEMENT",)
        is_contra = ptype in ("CONTRA_IN","CONTRA_OUT","JOURNAL_IN","JOURNAL_OUT")

        if is_disbursement:
            doc_title = "Payment Voucher"
            rows_html = (
                "<table><tbody>"
                f"<tr><td>Voucher No</td><td class='r'><b>{_html.escape(ref_no)}</b></td></tr>"
                f"<tr><td>Date</td><td class='r'>{_html.escape(dt)}</td></tr>"
                f"<tr><td>Paid To</td><td class='r'>{_html.escape(party)}</td></tr>"
                f"<tr><td>Amount</td><td class='r'><b>{_fmt(amount)}</b></td></tr>"
                f"<tr><td>Mode</td><td class='r'>{_html.escape(mode)}</td></tr>"
                f"<tr><td>Ref / UTR</td><td class='r'>{_html.escape(ref)}</td></tr>"
                f"<tr><td>Narration</td><td class='r'>{_html.escape(narr)}</td></tr>"
                "</tbody></table>"
            )
            msg = (
                f"Payment of {_fmt(amount)} to {party} recorded on {dt} by {mode}.\n"
                f"Voucher: {ref_no}. Ref: {ref}.\n"
                f"Narration: {narr}."
            )
        elif is_contra:
            doc_title = "Contra / Journal Entry"
            rows_html = (
                "<table><tbody>"
                f"<tr><td>Ref No</td><td class='r'><b>{_html.escape(ref_no)}</b></td></tr>"
                f"<tr><td>Date</td><td class='r'>{_html.escape(dt)}</td></tr>"
                f"<tr><td>Amount</td><td class='r'><b>{_fmt(amount)}</b></td></tr>"
                f"<tr><td>Mode</td><td class='r'>{_html.escape(mode)}</td></tr>"
                f"<tr><td>Narration</td><td class='r'>{_html.escape(narr)}</td></tr>"
                "</tbody></table>"
            )
            msg = (
                f"Cash contra entry of {_fmt(amount)} on {dt}.\n"
                f"Ref: {ref_no}. Narration: {narr}."
            )
        else:
            # Receipt / advance
            doc_title = "Payment Receipt"
            rows_html = (
                "<table><tbody>"
                f"<tr><td>Receipt No</td><td class='r'><b>{_html.escape(ref_no)}</b></td></tr>"
                f"<tr><td>Party</td><td class='r'>{_html.escape(party)}</td></tr>"
                f"<tr><td>Date</td><td class='r'>{_html.escape(dt)}</td></tr>"
                f"<tr><td>Mode</td><td class='r'>{_html.escape(mode)}</td></tr>"
                f"<tr><td>Amount Received</td><td class='r'><b>{_fmt(amount)}</b></td></tr>"
                f"<tr><td>Against Invoice</td><td class='r'>{_html.escape(inv_no or '—')}</td></tr>"
                f"<tr><td>Against Challan</td><td class='r'>{_html.escape(chal_no or '—')}</td></tr>"
                f"<tr><td>UTR / Ref</td><td class='r'>{_html.escape(ref)}</td></tr>"
                f"<tr><td>Narration</td><td class='r'>{_html.escape(narr)}</td></tr>"
                "</tbody></table>"
            )
            if mode and mode != "CASH":
                msg = (
                    f"Hello {party},\n"
                    f"Payment received: {_fmt(amount)} on {dt} via {mode}.\n"
                    f"UTR/Ref: {ref}. Receipt: {ref_no}. Thank you."
                )
            else:
                msg = (
                    f"Hello {party},\n"
                    f"Cash received: {_fmt(amount)} on {dt}.\n"
                    f"Receipt: {ref_no}. Thank you."
                )

        subtitle = (
            f"{_html.escape(doc_title)}<br>"
            f"Date: {_html.escape(dt)}<br>"
            f"Mode: {_html.escape(mode)}"
        )
        html_doc = _simple_doc_html(f"{doc_title} {ref_no}", subtitle, rows_html)
        safe_ref = _safe_key(ref_no)

        a1, a2, a3 = st.columns(3)
        with a1:
            _open_print_html(
                html_doc,
                f"{key}_{safe_ref}.html",
                f"{key}_print_{safe_ref}",
            )
        with a2:
            _render_wa_action(
                "📲 WhatsApp",
                rec.get("mobile"),
                msg,
                f"{key}_wa_{safe_ref}",
                party_name=party,
            )
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                rec.get("mobile"),
                msg,
                html_doc,
                f"{key}_{safe_ref}.html",
                f"{key}_wa_print_{safe_ref}",
                party_name=party,
            )


# ══════════════════════════════════════════════════════════════════════════════
# JOURNAL REGISTER ACTION DRAWER
# ══════════════════════════════════════════════════════════════════════════════

def _journal_action_drawer(df, key="jr_doc"):
    """Action drawer for accounting vouchers -- print + optional WA."""
    if df is None or df.empty or "Voucher No" not in df.columns:
        return

    with st.expander("🔎 Open Accounting Voucher / Print / WhatsApp", expanded=False):
        voucher_nos = [str(x) for x in df["Voucher No"].dropna().tolist() if str(x).strip()]
        if not voucher_nos:
            st.info("No vouchers in this period.")
            return

        sel_key = f"{key}_sel"
        if st.session_state.get(sel_key) not in voucher_nos:
            st.session_state[sel_key] = voucher_nos[0]
        vno = st.selectbox("Voucher", voucher_nos, key=sel_key)
        if not vno:
            return

        head = _q("""
            SELECT
                j.voucher_no, j.voucher_date::text, j.voucher_type,
                COALESCE(j.narration,'') AS narration,
                ROUND(j.total_debit,2)   AS total_debit,
                ROUND(j.total_credit,2)  AS total_credit,
                COALESCE(j.ref_doc_no,'') AS ref_doc_no,
                j.created_by
            FROM journal_entries j
            WHERE j.voucher_no = %(vno)s
            LIMIT 1
        """, {"vno": vno})

        if not head:
            st.warning("Voucher not found.")
            return

        jv = head[0]

        lines = _q("""
            SELECT
                jl.account_name,
                COALESCE(jl.debit,  0) AS dr_amount,
                COALESCE(jl.credit, 0) AS cr_amount,
                COALESCE(jl.narration,'') AS narration
            FROM journal_lines jl
            JOIN journal_entries j ON j.id = jl.journal_id
            WHERE j.voucher_no = %(vno)s
            ORDER BY jl.id
        """, {"vno": vno})

        c1, c2, c3 = st.columns(3)
        c1.metric("Type",    jv.get("voucher_type") or "—")
        c2.metric("Total Dr", _fmt(jv.get("total_debit")))
        c3.metric("Total Cr", _fmt(jv.get("total_credit")))

        # Print HTML
        line_rows = "".join(
            "<tr>"
            f"<td>{_html.escape(str(l.get('account_name') or ''))}</td>"
            f"<td class='r'>{_fmt(l.get('dr_amount')) if float(l.get('dr_amount') or 0) else ''}</td>"
            f"<td class='r'>{_fmt(l.get('cr_amount')) if float(l.get('cr_amount') or 0) else ''}</td>"
            f"<td>{_html.escape(str(l.get('narration') or ''))}</td>"
            "</tr>"
            for l in lines
        )
        table = (
            "<table><thead><tr><th>Account</th>"
            "<th class='r'>Dr</th><th class='r'>Cr</th><th>Narration</th></tr></thead>"
            f"<tbody>{line_rows}</tbody></table>"
        )
        totals = (
            "<div class='tot'>"
            f"<div><span>Total Dr</span><b>{_fmt(jv.get('total_debit'))}</b></div>"
            f"<div><span>Total Cr</span><b>{_fmt(jv.get('total_credit'))}</b></div>"
            "</div>"
        )
        subtitle = (
            f"Date: {_html.escape(str(jv.get('voucher_date') or ''))}<br>"
            f"Type: {_html.escape(str(jv.get('voucher_type') or ''))}<br>"
            f"Narration: {_html.escape(str(jv.get('narration') or ''))}<br>"
            f"Ref Doc: {_html.escape(str(jv.get('ref_doc_no') or ''))}"
        )
        html_doc = _simple_doc_html(f"Journal Voucher {vno}", subtitle, table, totals)

        msg = (
            f"Journal Voucher {vno} dated {jv.get('voucher_date') or ''}.\n"
            f"Type: {jv.get('voucher_type') or ''}. Narration: {jv.get('narration') or ''}.\n"
            f"Ref doc: {jv.get('ref_doc_no') or '—'}.\n"
            f"Dr {_fmt(jv.get('total_debit'))} / Cr {_fmt(jv.get('total_credit'))}."
        )

        # Try to get a party mobile from journal lines
        mobile = ""
        journal_party = ""
        if lines:
            # First account with a matching party in parties table
            acct_names = [str(l.get("account_name") or "") for l in lines if l.get("account_name")]
            for acct in acct_names:
                mob_rows = _q(
                    "SELECT party_name, mobile FROM parties WHERE party_name ILIKE %(n)s LIMIT 1",
                    {"n": acct},
                )
                if mob_rows:
                    journal_party = str(mob_rows[0].get("party_name") or acct)
                if mob_rows and mob_rows[0].get("mobile"):
                    mobile = str(mob_rows[0]["mobile"])
                    break

        safe_vno = _safe_key(vno)
        a1, a2, a3 = st.columns(3)
        with a1:
            _open_print_html(html_doc, f"journal_{safe_vno}.html", f"{key}_print_{safe_vno}")
        with a2:
            _render_wa_action("📲 WhatsApp", mobile, msg, f"{key}_wa_{safe_vno}", party_name=journal_party)
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                mobile,
                msg,
                html_doc,
                f"journal_{safe_vno}.html",
                f"{key}_wa_print_{safe_vno}",
                party_name=journal_party,
            )
        if not mobile:
            with st.expander("Copy message", expanded=False):
                st.text_area("Journal voucher message", msg, height=120,
                             key=f"{key}_copy_{safe_vno}")


# ══════════════════════════════════════════════════════════════════════════════
# PARTY LEDGER DOCUMENT ACTION DRAWER
# ══════════════════════════════════════════════════════════════════════════════

def _ledger_doc_action_drawer(df, party_name, key="pl_doc"):
    """Open any ledger row as a printable document + WA send.

    Detects doc type from the 'Type' and 'Ref No' columns.
    Separate from the mini-ledger statement WA (which stays unchanged).
    """
    if df is None or df.empty:
        return
    if "Ref No" not in df.columns or "Type" not in df.columns:
        return

    with st.expander("🔎 Open Ledger Document / Print / WhatsApp", expanded=False):
        ref_nos = [str(r) for r in df["Ref No"].dropna().tolist() if str(r).strip()]
        if not ref_nos:
            st.info("No documents in this period.")
            return

        sel_key = f"{key}_sel"
        if st.session_state.get(sel_key) not in ref_nos:
            st.session_state[sel_key] = ref_nos[0]
        ref_no = st.selectbox("Select document", ref_nos, key=sel_key)
        if not ref_no:
            return

        # Detect type from df row
        row_mask = df["Ref No"].astype(str) == ref_no
        dtype = ""
        if row_mask.any():
            dtype = str(df.loc[row_mask, "Type"].iloc[0]).upper()

        html_doc = ""
        msg      = ""
        mobile   = ""

        ref_upper = ref_no.upper()

        # ── SALES INVOICE ─────────────────────────────────────────────────────
        if dtype == "INVOICE" or ref_upper.startswith("INV/"):
            inv_rows = _q("""
                SELECT i.invoice_no, i.invoice_date::text, i.grand_total,
                       COALESCE(i.amount_paid,0) AS amount_paid,
                       COALESCE(i.balance_due, i.grand_total) AS balance_due,
                       COALESCE(i.payment_status,'UNPAID') AS payment_status,
                       COALESCE(p.party_name, o.party_name, o.patient_name, '') AS party_name,
                       COALESCE(p.mobile, o.patient_mobile, '') AS mobile
                FROM invoices i
                LEFT JOIN parties p ON p.id = i.party_id
                LEFT JOIN LATERAL (
                    SELECT o2.party_name, o2.patient_name, o2.patient_mobile
                    FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
                ) o ON TRUE
                WHERE i.invoice_no = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if inv_rows:
                inv = inv_rows[0]
                mobile = inv.get("mobile") or ""
                msg = (
                    f"Hello {inv.get('party_name') or party_name},\n"
                    f"Invoice {ref_no} dated {inv.get('invoice_date') or ''} is {_fmt(inv.get('grand_total'))}.\n"
                    f"Paid: {_fmt(inv.get('amount_paid'))}. Balance: {_fmt(inv.get('balance_due'))}."
                )
                try:
                    from modules.billing.smart_print import render_smart_invoice
                    html_doc = render_smart_invoice(ref_no, return_html=True) or ""
                except Exception:
                    html_doc = ""

        # ── CHALLAN ───────────────────────────────────────────────────────────
        elif dtype == "CHALLAN" or ref_upper.startswith("CH/"):
            ch_rows = _q("""
                SELECT c.challan_no, c.challan_date::text, c.grand_total,
                       c.status,
                       COALESCE(p.party_name, o.party_name, o.patient_name, '') AS party_name,
                       COALESCE(p.mobile, o.patient_mobile, '') AS mobile
                FROM challans c
                LEFT JOIN parties p ON p.id = c.party_id
                LEFT JOIN LATERAL (
                    SELECT o2.party_name, o2.patient_name, o2.patient_mobile
                    FROM orders o2 WHERE o2.id::text = ANY(c.order_ids) LIMIT 1
                ) o ON TRUE
                WHERE c.challan_no = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if ch_rows:
                ch = ch_rows[0]
                mobile = ch.get("mobile") or ""
                msg = (
                    f"Hello {ch.get('party_name') or party_name},\n"
                    f"Challan {ref_no} dated {ch.get('challan_date') or ''}: {_fmt(ch.get('grand_total'))}.\n"
                    f"Status: {ch.get('status') or '—'}."
                )
                try:
                    from modules.billing.smart_print import render_smart_challan
                    html_doc = render_smart_challan(ref_no, return_html=True) or ""
                except Exception:
                    html_doc = ""

        # ── PURCHASE INVOICE ─────────────────────────────────────────────────
        elif dtype == "PURCHASE_INVOICE":
            pi_rows = _q("""
                SELECT pi.invoice_no, pi.invoice_date::text, pi.invoice_total,
                       COALESCE(pi.amount_paid, 0)   AS amount_paid,
                       COALESCE(pi.balance_due, pi.invoice_total) AS balance_due,
                       COALESCE(pi.payment_status,'UNPAID') AS payment_status,
                       pi.supplier_name,
                       COALESCE(p.mobile,'') AS mobile
                FROM purchase_invoices pi
                LEFT JOIN parties p ON p.id::text = pi.supplier_id::text
                WHERE pi.invoice_no = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if pi_rows:
                pi     = pi_rows[0]
                mobile = pi.get("mobile") or ""
                msg = (
                    f"Purchase invoice {ref_no} dated {pi.get('invoice_date') or ''}: "
                    f"total {_fmt(pi.get('invoice_total'))}, "
                    f"balance {_fmt(pi.get('balance_due'))}."
                )
                lines_pi = _q("""
                    SELECT product_name, eye_side, received_qty,
                           actual_price, gst_percent, line_total
                    FROM purchase_invoice_lines
                    WHERE invoice_no = %(rno)s ORDER BY item_no
                """, {"rno": ref_no})
                body_rows = "".join(
                    "<tr><td>{}</td><td>{}</td><td class='r'>{}</td>"
                    "<td class='r'>{}</td><td class='r'>{}</td></tr>".format(
                        _html.escape(str(l.get("product_name") or "")),
                        _html.escape(str(l.get("eye_side") or "")),
                        l.get("received_qty") or 0,
                        _fmt(l.get("actual_price")),
                        _fmt(l.get("line_total")),
                    )
                    for l in lines_pi
                )
                table = (
                    "<table><thead><tr><th>Product</th><th>Eye</th>"
                    "<th class='r'>Qty</th><th class='r'>Rate</th>"
                    "<th class='r'>Total</th></tr></thead>"
                    f"<tbody>{body_rows}</tbody></table>"
                )
                totals = (
                    "<div class='tot'>"
                    f"<div class='grand'><span>Invoice Total</span>"
                    f"<b>{_fmt(pi.get('invoice_total'))}</b></div>"
                    "</div>"
                )
                subtitle = (
                    f"Supplier: {_html.escape(str(pi.get('supplier_name') or ''))}<br>"
                    f"Date: {_html.escape(str(pi.get('invoice_date') or ''))}<br>"
                    f"Status: {_html.escape(str(pi.get('payment_status') or ''))}"
                )
                html_doc = _simple_doc_html(f"Purchase Invoice {ref_no}", subtitle, table, totals)

        # ── SUPPLIER CHALLAN (purchase acknowledgement) ───────────────────────
        elif dtype == "SUPPLIER_CHALLAN":
            pa_rows = _q("""
                SELECT
                    COALESCE(NULLIF(pa.challan_no,''), 'PA-' || pa.id::text) AS ref_no,
                    COALESCE(pa.document_date::text, pa.created_at::date::text) AS doc_date,
                    pa.supplier_name,
                    COALESCE(pa.order_no,'') AS order_no,
                    COALESCE(pa.invoice_no,'') AS invoice_no,
                    ROUND(COALESCE(pa.purchase_price,0) * COALESCE(pa.qty,1), 2) AS total_value,
                    COALESCE(pa.billing_status,'PENDING') AS billing_status,
                    COALESCE(p.mobile,'') AS mobile
                FROM purchase_acknowledgements pa
                LEFT JOIN parties p ON p.party_name ILIKE pa.supplier_name
                WHERE COALESCE(NULLIF(pa.challan_no,''), 'PA-' || pa.id::text) = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if pa_rows:
                pa     = pa_rows[0]
                mobile = pa.get("mobile") or ""
                msg = (
                    f"Supplier challan {ref_no} dated {pa.get('doc_date') or ''}: "
                    f"{_fmt(pa.get('total_value'))}. "
                    f"Status: {pa.get('billing_status') or '—'}."
                )
                rows_html = (
                    "<table><tbody>"
                    f"<tr><td>Challan No</td><td class='r'><b>{_html.escape(ref_no)}</b></td></tr>"
                    f"<tr><td>Supplier</td><td class='r'>{_html.escape(str(pa.get('supplier_name') or ''))}</td></tr>"
                    f"<tr><td>Date</td><td class='r'>{_html.escape(str(pa.get('doc_date') or ''))}</td></tr>"
                    f"<tr><td>Order No</td><td class='r'>{_html.escape(str(pa.get('order_no') or '—'))}</td></tr>"
                    f"<tr><td>Invoice No</td><td class='r'>{_html.escape(str(pa.get('invoice_no') or '—'))}</td></tr>"
                    f"<tr><td>Total Value</td><td class='r'><b>{_fmt(pa.get('total_value'))}</b></td></tr>"
                    f"<tr><td>Billing Status</td><td class='r'>{_html.escape(str(pa.get('billing_status') or ''))}</td></tr>"
                    "</tbody></table>"
                )
                subtitle = (
                    f"Supplier: {_html.escape(str(pa.get('supplier_name') or ''))}<br>"
                    f"Date: {_html.escape(str(pa.get('doc_date') or ''))}"
                )
                html_doc = _simple_doc_html(f"Supplier Challan {ref_no}", subtitle, rows_html)

        # ── PAYMENT / RECEIPT ─────────────────────────────────────────────────
        elif dtype in ("PAYMENT","RECEIPT","ADVANCE","SUPPLIER_PAYMENT","DISBURSEMENT") or \
             any(ref_upper.startswith(p) for p in ("PAY/","PMT/","CPR-","DISB/")):
            pay_rows = _q("""
                SELECT
                    COALESCE(NULLIF(p.payment_no,''), p.id::text) AS ref_no,
                    p.payment_date::text,
                    p.payment_type,
                    COALESCE(NULLIF(p.payment_mode,''), p.method, '') AS mode,
                    ROUND(p.amount,2) AS amount,
                    COALESCE(p.party_name, ip.party_name, cp.party_name,
                        (SELECT COALESCE(NULLIF(o.party_name,''), NULLIF(o.patient_name,''))
                         FROM orders o
                         WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
                            OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
                         LIMIT 1), '') AS party_name,
                    COALESCE(ip.mobile, cp.mobile,
                        (SELECT o.patient_mobile FROM orders o
                         WHERE (p.order_id IS NOT NULL AND o.id = p.order_id)
                            OR (p.advance_for_order_id IS NOT NULL AND o.id = p.advance_for_order_id)
                         LIMIT 1), '') AS mobile,
                    COALESCE(i.invoice_no,'') AS invoice_no,
                    COALESCE(p.remarks,'') AS remarks
                FROM payments p
                LEFT JOIN invoices i ON i.id = p.invoice_id
                LEFT JOIN challans c ON c.id = p.challan_id
                LEFT JOIN parties ip ON ip.id = i.party_id
                LEFT JOIN parties cp ON cp.id = c.party_id
                WHERE COALESCE(NULLIF(p.payment_no,''), p.id::text) = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if pay_rows:
                rec = pay_rows[0]
                mobile  = rec.get("mobile") or ""
                ptype   = str(rec.get("payment_type") or "PAYMENT").upper()
                pmode   = str(rec.get("mode") or "").upper()
                pparty  = rec.get("party_name") or party_name
                pamount = rec.get("amount") or 0
                pdt     = rec.get("payment_date") or ""
                pinv    = rec.get("invoice_no") or ""
                pnarr   = rec.get("remarks") or ""

                if ptype in ("DISBURSEMENT","SUPPLIER_PAYMENT"):
                    msg = (
                        f"Payment of {_fmt(pamount)} to {pparty} recorded on {pdt}.\n"
                        f"Voucher: {ref_no}."
                    )
                    doc_title = "Payment Voucher"
                else:
                    msg = (
                        f"Hello {pparty},\n"
                        f"Payment received: {_fmt(pamount)} on {pdt}.\n"
                        f"Receipt: {ref_no}. Thank you."
                    )
                    doc_title = "Payment Receipt"

                detail = (
                    f"Party: {_html.escape(str(pparty))}<br>"
                    f"Date: {_html.escape(str(pdt))}<br>"
                    f"Mode: {_html.escape(str(pmode))}"
                )
                rows_html = (
                    "<table><tbody>"
                    f"<tr><td>Document No</td><td class='r'><b>{_html.escape(ref_no)}</b></td></tr>"
                    f"<tr><td>Amount</td><td class='r'><b>{_fmt(pamount)}</b></td></tr>"
                    f"<tr><td>Mode</td><td class='r'>{_html.escape(pmode)}</td></tr>"
                    f"<tr><td>Against Invoice</td><td class='r'>{_html.escape(pinv or '—')}</td></tr>"
                    f"<tr><td>Narration</td><td class='r'>{_html.escape(pnarr)}</td></tr>"
                    "</tbody></table>"
                )
                html_doc = _simple_doc_html(f"{doc_title} {ref_no}", detail, rows_html)

        # ── JOURNAL VOUCHER ───────────────────────────────────────────────────
        else:
            jv_rows = _q("""
                SELECT j.voucher_no, j.voucher_date::text, j.voucher_type,
                       COALESCE(j.narration,'') AS narration,
                       ROUND(j.total_debit,2)   AS total_debit,
                       ROUND(j.total_credit,2)  AS total_credit,
                       COALESCE(j.ref_doc_no,'') AS ref_doc_no
                FROM journal_entries j
                WHERE j.voucher_no = %(rno)s
                LIMIT 1
            """, {"rno": ref_no})
            if jv_rows:
                jv = jv_rows[0]
                msg = (
                    f"Journal Voucher {ref_no} dated {jv.get('voucher_date') or ''}.\n"
                    f"Narration: {jv.get('narration') or '—'}. "
                    f"Dr {_fmt(jv.get('total_debit'))} / Cr {_fmt(jv.get('total_credit'))}."
                )
                subtitle = (
                    f"Date: {_html.escape(str(jv.get('voucher_date') or ''))}<br>"
                    f"Type: {_html.escape(str(jv.get('voucher_type') or ''))}<br>"
                    f"Narration: {_html.escape(str(jv.get('narration') or ''))}"
                )
                html_doc = _simple_doc_html(f"Journal Voucher {ref_no}", subtitle, "")

        # ── Render buttons ────────────────────────────────────────────────────
        if not msg:
            st.info(f"Could not load details for {ref_no}.")
            return

        safe_ref = _safe_key(ref_no)
        a1, a2, a3 = st.columns(3)
        with a1:
            if html_doc:
                _open_print_html(html_doc, f"ldoc_{safe_ref}.html", f"{key}_print_{safe_ref}")
            else:
                st.button("🖨️ Print / Save PDF", disabled=True,
                          key=f"{key}_print_dis_{safe_ref}",
                          use_container_width=True,
                          help="Print template unavailable for this document type.")
        with a2:
            _render_wa_action("📲 WhatsApp", mobile, msg, f"{key}_wa_{safe_ref}", party_name=party_name)
        with a3:
            _wa_with_print_link(
                "📲 WA + Print Link",
                mobile,
                msg,
                html_doc,
                f"ldoc_{safe_ref}.html",
                f"{key}_wa_print_{safe_ref}",
                party_name=party_name,
            )
        if dtype in ("PURCHASE_INVOICE", "SUPPLIER_CHALLAN"):
            _render_uploaded_purchase_invoice_download(
                ref_no,
                party_name=party_name,
                key=f"{key}_upl_{safe_ref}",
            )


def render_disbursement_book():
    st.caption("All outgoing payments -- supplier payments and expenses")
    fd, td   = _date_filter("db")
    party    = _party_filter("db", "All Payees")
    grouping = _grouping("db")
    cat_opts = ["All","SUPPLIER","EXPENSE","SALARY","RENT","ELECTRICITY","OTHER"]
    cat      = st.selectbox("Category", cat_opts, key="db_cat")

    pf = "AND p.party_name ILIKE %(pty)s" if party else ""
    cf = "" if cat == "All" else "AND UPPER(COALESCE(p.remarks,'')) LIKE %(cat)s"

    rows = _q("""
        SELECT
            p.payment_date::text         AS "Date",
            p.payment_no                 AS "Voucher No",
            COALESCE(p.party_name,'')    AS "Payee",
            COALESCE(NULLIF(p.payment_mode,''), p.method, '') AS "Mode",
            COALESCE(p.reference_no,'')  AS "Ref",
            ROUND(p.amount,2)            AS "Amount (₹)",
            COALESCE(p.remarks,'')       AS "Narration"
        FROM payments p
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND p.payment_type = 'DISBURSEMENT'
          AND NOT COALESCE(p.is_deleted,FALSE)
          """+ (pf or "") + """ """+ (cf or "") + """
        ORDER BY p.payment_date DESC
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%", "cat": f"%{cat}%"})

    if not rows:
        st.info("No disbursements in this period."); return

    df = _df(rows)
    df["Amount (₹)"] = pd.to_numeric(df["Amount (₹)"], errors="coerce").fillna(0)

    _metrics(("Entries", str(len(df))), ("Total Paid Out", _fmt(df["Amount (₹)"].sum())))
    display = _apply_grouping(df, grouping, "Amount (₹)", "Amount (₹)")
    _render_register_grid(
        display,
        "db",
        select_col="Voucher No",
        select_state_key="db_doc_sel",
        column_config={"Amount (₹)": st.column_config.NumberColumn(format="₹%.2f")},
    )
    _payment_action_drawer(df.rename(columns={"Voucher No": "Ref No"}) if "Voucher No" in df.columns else df, "Ref No", "db_doc", "Disbursement")
    _export(df, f"Disbursement_Book_{fd}_{td}", "db_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 5. CASH BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_cash_book():
    st.caption("Cash receipts and payments -- daily running balance")
    fd, td   = _date_filter("cb")
    grouping = _grouping("cb")
    party = _party_filter("cb", "All Parties / Customers", include_patients=True)
    pf = "AND party_x.party_name ILIKE %(pty)s" if party else ""
    find = st.text_input(
        "Find receipt / party / narration",
        key="cb_find",
        placeholder="Receipt no, patient, party, narration...",
    ).strip()
    sf = """
        AND (
            COALESCE(NULLIF(p.payment_no,''), p.id::text) ILIKE %(find)s
            OR party_x.party_name ILIKE %(find)s
            OR COALESCE(p.reference_no,'') ILIKE %(find)s
            OR COALESCE(p.remarks,'') ILIKE %(find)s
        )
    """ if find else ""
    scope_filter = _payment_scope_clause("cb")

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            COALESCE(NULLIF(p.payment_no,''), p.id::text) AS "Ref No",
            party_x.party_name           AS "Party",
            CASE WHEN COALESCE(NULLIF(p.payment_type,''), 'PAYMENT') IN ('PAYMENT','RECEIPT','ADVANCE','OPENING','CONTRA_IN','JOURNAL_IN') THEN ROUND(p.amount,2) ELSE 0 END AS "Receipts (₹)",
            CASE WHEN COALESCE(NULLIF(p.payment_type,''), 'PAYMENT') IN ('DISBURSEMENT','CONTRA_OUT','JOURNAL_OUT') THEN ROUND(p.amount,2) ELSE 0 END AS "Payments (₹)",
            {_PAY_NARRATION_EXPR}        AS "Narration"
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN challans c ON c.id = p.challan_id
        LEFT JOIN parties ip ON ip.id = i.party_id
        LEFT JOIN parties cp ON cp.id = c.party_id
        CROSS JOIN LATERAL (
            SELECT {_PAY_PARTY_EXPR} AS party_name
        ) party_x
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) = 'CASH'
          AND NOT COALESCE(p.is_deleted,FALSE)
          """ + pf + """ """ + sf + """ """ + scope_filter + """
        ORDER BY p.payment_date ASC, p.created_at ASC
    """, {
        "fd": fd,
        "td": td,
        "pty": f"%{party or ''}%",

    })

    if not rows:
        st.info("No cash transactions in this period."); return

    df = _df(rows)
    for c in ["Receipts (₹)","Payments (₹)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Running balance
    df["Balance (₹)"] = (df["Receipts (₹)"] - df["Payments (₹)"]).cumsum()

    total_in  = df["Receipts (₹)"].sum()
    total_out = df["Payments (₹)"].sum()
    _metrics(
        ("Cash In",  _fmt(total_in)),
        ("Cash Out", _fmt(total_out)),
        ("Balance",  _fmt(total_in - total_out)),
        ("Entries",  str(len(df))),
    )

    if grouping == "Detail":
        _render_register_grid(
            df,
            "cb",
            select_col="Ref No",
            select_state_key="cb_doc_sel",
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Receipts (₹)","Payments (₹)","Balance (₹)"]},
        )
    else:
        display = _apply_grouping(df, grouping, "Receipts (₹)", "Payments (₹)")
        st.dataframe(display, width='stretch', hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in display.select_dtypes("number").columns})
    _payment_action_drawer(df, "Ref No", "cb_doc", "Cash Book")
    _export(df, f"Cash_Book_{fd}_{td}", "cb_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 6. BANK BOOK
# ══════════════════════════════════════════════════════════════════════════════

def render_bank_book():
    st.caption("Bank receipts and payments -- UPI, NEFT, CHEQUE, RTGS")
    fd, td   = _date_filter("bb2")
    bank_modes = ["All Bank Modes","UPI","NEFT","CHEQUE","RTGS","CARD"]
    mode     = st.selectbox("Bank Mode", bank_modes, key="bb2_mode")
    grouping = _grouping("bb2")

    mf = "" if mode == "All Bank Modes" else "AND UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) = %(mode)s"
    party = _party_filter("bb2", "All Parties / Customers", include_patients=True)
    pf = "AND party_x.party_name ILIKE %(pty)s" if party else ""
    scope_filter = _payment_scope_clause("bb2")

    rows = _q(f"""
        SELECT
            p.payment_date::text         AS "Date",
            COALESCE(NULLIF(p.payment_no,''), p.id::text) AS "Ref No",
            party_x.party_name           AS "Party",
            UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) AS "Mode",
            COALESCE(p.reference_no,'')  AS "UTR / Cheque No",
            CASE WHEN COALESCE(NULLIF(p.payment_type,''), 'PAYMENT') IN ('PAYMENT','RECEIPT','ADVANCE','OPENING','CONTRA_IN','JOURNAL_IN') THEN ROUND(p.amount,2) ELSE 0 END AS "Receipts (₹)",
            CASE WHEN COALESCE(NULLIF(p.payment_type,''), 'PAYMENT') IN ('DISBURSEMENT','CONTRA_OUT','JOURNAL_OUT') THEN ROUND(p.amount,2) ELSE 0 END AS "Payments (₹)",
            {_PAY_NARRATION_EXPR}        AS "Narration"
        FROM payments p
        LEFT JOIN invoices i ON i.id = p.invoice_id
        LEFT JOIN challans c ON c.id = p.challan_id
        LEFT JOIN parties ip ON ip.id = i.party_id
        LEFT JOIN parties cp ON cp.id = c.party_id
        CROSS JOIN LATERAL (
            SELECT {_PAY_PARTY_EXPR} AS party_name
        ) party_x
        WHERE p.payment_date BETWEEN %(fd)s AND %(td)s
          AND UPPER(COALESCE(NULLIF(p.payment_mode,''), p.method, '')) != 'CASH'
          AND NOT COALESCE(p.is_deleted,FALSE)
          """+ (mf or "") + """ """+ (pf or "") + """ """+ scope_filter + """
        ORDER BY p.payment_date ASC, p.created_at ASC
    """, {"fd": fd, "td": td, "mode": mode, "pty": f"%{party or ''}%"})

    if not rows:
        st.info("No bank transactions in this period."); return

    df = _df(rows)
    for c in ["Receipts (₹)","Payments (₹)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["Balance (₹)"] = (df["Receipts (₹)"] - df["Payments (₹)"]).cumsum()

    _metrics(
        ("Bank In",  _fmt(df["Receipts (₹)"].sum())),
        ("Bank Out", _fmt(df["Payments (₹)"].sum())),
        ("Balance",  _fmt(df["Receipts (₹)"].sum() - df["Payments (₹)"].sum())),
    )
    if grouping == "Detail":
        _render_register_grid(
            df,
            "bb2",
            select_col="Ref No",
            select_state_key="bb2_doc_sel",
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["Receipts (₹)","Payments (₹)","Balance (₹)"]},
        )
    else:
        st.dataframe(_apply_grouping(df, grouping, "Receipts (₹)", "Payments (₹)"),
                     width='stretch', hide_index=True)
    _payment_action_drawer(df, "Ref No", "bb2_doc", "Bank Book")
    _export(df, f"Bank_Book_{fd}_{td}", "bb2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 7. PARTY LEDGER
# ══════════════════════════════════════════════════════════════════════════════

def render_party_ledger():
    st.caption("Single account ledger. Financial = tax invoice truth. Real = operational challan + invoice view.")
    fd, td = _date_filter("pl2")
    ledger_mode = st.radio(
        "Ledger account",
        ["Wholesale / CRM Parties", "Retail Customers", "Suppliers"],
        horizontal=True,
        key="pl2_mode",
    )
    ledger_basis = st.radio(
        "Ledger basis",
        ["Financial Ledger", "Real Ledger"],
        horizontal=True,
        key="pl2_basis",
        help="Financial uses final accounting documents only. Real also shows open challans; converted challans are suppressed so invoice remains single truth.",
    )

    # Party selection -- required
    @st.cache_data(ttl=120, show_spinner=False)
    def _all_parties(mode, active_only):
        if active_only:
            if mode == "Suppliers":
                rows = _q("""
                    SELECT party_name FROM (
                        SELECT supplier_name AS party_name
                        FROM purchase_invoices
                        WHERE COALESCE(supplier_name,'') <> ''
                          AND NOT COALESCE(is_deleted,FALSE)
                        UNION
                        SELECT supplier_name AS party_name
                        FROM purchase_acknowledgements
                        WHERE COALESCE(supplier_name,'') <> ''
                        UNION
                        SELECT party_name
                        FROM payments
                        WHERE payment_type = 'DISBURSEMENT'
                          AND COALESCE(party_name,'') <> ''
                          AND NOT COALESCE(is_deleted,FALSE)
                    ) x
                    WHERE COALESCE(party_name,'') <> ''
                    ORDER BY party_name
                """)
                return [x["party_name"] for x in rows]
            if mode == "Wholesale / CRM Parties":
                rows = _q("""
                    SELECT party_name FROM (
                        SELECT COALESCE(pt.party_name, o.party_name) AS party_name
                        FROM invoices i
                        LEFT JOIN parties pt ON pt.id = i.party_id
                        LEFT JOIN LATERAL (
                            SELECT o2.party_name, o2.order_type
                            FROM orders o2
                            WHERE o2.id::text = ANY(i.order_ids)
                            LIMIT 1
                        ) o ON TRUE
                        WHERE COALESCE(i.is_deleted,FALSE)=FALSE
                          AND UPPER(COALESCE(o.order_type,'WHOLESALE')) NOT IN ('RETAIL','ONLINE')
                        UNION
                        SELECT COALESCE(pt.party_name, o.party_name) AS party_name
                        FROM challans c
                        LEFT JOIN parties pt ON pt.id = c.party_id
                        LEFT JOIN LATERAL (
                            SELECT o2.party_name, o2.order_type
                            FROM orders o2
                            WHERE o2.id::text = ANY(c.order_ids)
                            LIMIT 1
                        ) o ON TRUE
                        WHERE COALESCE(c.is_deleted,FALSE)=FALSE
                          AND UPPER(COALESCE(o.order_type,'WHOLESALE')) NOT IN ('RETAIL','ONLINE')
                        UNION
                        SELECT party_x.party_name
                        FROM payments p
                        LEFT JOIN invoices i ON i.id = p.invoice_id
                        LEFT JOIN challans c ON c.id = p.challan_id
                        LEFT JOIN parties ip ON ip.id = i.party_id
                        LEFT JOIN parties cp ON cp.id = c.party_id
                        CROSS JOIN LATERAL (SELECT """ + _PAY_PARTY_EXPR + """ AS party_name) party_x
                        WHERE p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
                          AND COALESCE(party_x.party_name,'') <> ''
                          AND NOT COALESCE(p.is_deleted,FALSE)
                          AND NOT EXISTS (
                              SELECT 1 FROM orders ox
                              WHERE ((p.order_id IS NOT NULL AND ox.id = p.order_id)
                                  OR (p.advance_for_order_id IS NOT NULL AND ox.id = p.advance_for_order_id)
                                  OR (i.order_ids IS NOT NULL AND ox.id::text = ANY(i.order_ids::text[]))
                                  OR (c.order_ids IS NOT NULL AND ox.id::text = ANY(c.order_ids::text[])))
                                AND UPPER(COALESCE(ox.order_type,'')) IN ('RETAIL','ONLINE')
                          )
                    ) x
                    WHERE COALESCE(party_name,'') <> ''
                    ORDER BY party_name
                """)
                return [x["party_name"] for x in rows]
            rows = _q("""
                SELECT party_name FROM (
                    SELECT COALESCE(o.patient_name, o.party_name) AS party_name
                    FROM orders o
                    WHERE UPPER(COALESCE(o.order_type,'')) = 'RETAIL'
                      AND COALESCE(o.patient_name, o.party_name, '') <> ''
                    UNION
                    SELECT COALESCE(o.patient_name, o.party_name) AS party_name
                    FROM invoices i
                    JOIN LATERAL (
                        SELECT o2.patient_name, o2.party_name, o2.order_type
                        FROM orders o2
                        WHERE o2.id::text = ANY(i.order_ids)
                        LIMIT 1
                    ) o ON TRUE
                    WHERE COALESCE(i.is_deleted,FALSE)=FALSE
                      AND UPPER(COALESCE(o.order_type,'')) = 'RETAIL'
                    UNION
                    SELECT COALESCE(o.patient_name, o.party_name) AS party_name
                    FROM payments p
                    JOIN orders o ON o.id = COALESCE(p.order_id, p.advance_for_order_id)
                    WHERE p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
                      AND UPPER(COALESCE(o.order_type,'')) = 'RETAIL'
                      AND NOT COALESCE(p.is_deleted,FALSE)
                ) x
                WHERE COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            return [x["party_name"] for x in rows]

        if mode == "Suppliers":
            rows = _q("""
                SELECT party_name FROM (
                    SELECT supplier_name AS party_name FROM purchase_invoices
                    WHERE COALESCE(supplier_name,'') <> ''
                      AND NOT COALESCE(is_deleted,FALSE)
                    UNION
                    SELECT supplier_name AS party_name FROM purchase_acknowledgements
                    WHERE COALESCE(supplier_name,'') <> ''
                    UNION
                    SELECT party_name FROM parties
                    WHERE COALESCE(is_active,TRUE)=TRUE
                      AND COALESCE(party_name,'') <> ''
                ) x
                WHERE COALESCE(party_name,'') <> ''
                ORDER BY party_name
            """)
            return [x["party_name"] for x in rows]
        if mode == "Wholesale / CRM Parties":
            rows = _q("""
                SELECT party_name
                FROM parties
                WHERE COALESCE(is_active,TRUE)=TRUE
                  AND COALESCE(party_name, '') <> ''
                ORDER BY party_name
            """)
            return [x["party_name"] for x in rows]
        rows = _q("""
            SELECT party_name FROM (
                SELECT COALESCE(patient_name, party_name) AS party_name
                FROM orders
                WHERE order_type = 'RETAIL'
                  AND COALESCE(patient_name, party_name, '') <> ''
                UNION
                SELECT COALESCE(master_name, '') AS party_name
                FROM patients
                WHERE COALESCE(master_name, '') <> ''
            ) x
            WHERE COALESCE(party_name, '') <> ''
            ORDER BY party_name
        """)
        return [x["party_name"] for x in rows]

    def _on_chg():
        st.session_state["pl2_term"] = st.session_state.get("pl2_input","")

    st.text_input("🔍 Search party", key="pl2_input", placeholder="Type name…", on_change=_on_chg)
    list_mode = st.radio(
        "Account list",
        ["Active ledger only", "All accounts"],
        horizontal=True,
        key="pl2_active_only",
        help="Active ledger only shows accounts with invoice, challan, payment, or supplier movement.",
    )
    term     = st.session_state.get("pl2_term","")
    names    = _all_parties(ledger_mode, list_mode == "Active ledger only")
    filtered = [n for n in names if term.lower() in n.lower()] if term else names
    _label_kind = "supplier" if ledger_mode == "Suppliers" else ("party" if ledger_mode.startswith("Wholesale") else "retail customer")
    placeholder = f"-- Select {_label_kind} ({len(filtered)}) --"
    chosen   = st.selectbox("Account *", [placeholder] + filtered, key="pl2_sel")

    if chosen == placeholder:
        st.info("Select an account to view its ledger.")
        return

    if ledger_mode == "Suppliers":
        _render_supplier_ledger_body(chosen, fd, td, ledger_basis)
        return

    real_challan_union = ""
    real_challan_union_open = ""
    if ledger_basis == "Real Ledger":
        real_challan_union = f"""
            UNION ALL

            SELECT
                c.challan_date::date AS entry_date,
                'CHALLAN'::text      AS entry_type,
                c.challan_no::text   AS ref_no,
                COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS party_name,
                COALESCE(c.grand_total, 0)::numeric AS debit,
                0::numeric AS credit,
                ('Operational Challan ' || COALESCE(c.challan_no,'') ||
                 CASE WHEN inv.invoice_no IS NOT NULL THEN ' -> Invoice ' || inv.invoice_no ELSE '' END) AS narration,
                COALESCE(c.created_by, '') AS created_by,
                c.created_at
            FROM challans c
            LEFT JOIN parties pt ON pt.id = c.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name
                FROM orders o2
                WHERE o2.id::text = ANY(c.order_ids)
                LIMIT 1
            ) o ON TRUE
            LEFT JOIN LATERAL (
                SELECT i2.invoice_no
                FROM invoices i2
                WHERE COALESCE(i2.is_deleted,FALSE)=FALSE
                  AND UPPER(COALESCE(i2.status,'')) NOT IN ('VOID','CANCELLED')
                  AND (
                      i2.challan_id = c.id
                      OR i2.order_ids::text[] && c.order_ids::text[]
                  )
                LIMIT 1
            ) inv ON TRUE
            WHERE COALESCE(c.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(c.status,'')) NOT IN ('CANCELLED','VOID')
              AND inv.invoice_no IS NULL
        """
        real_challan_union_open = """
            UNION ALL

            SELECT
                c.challan_date::date AS entry_date,
                COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS party_name,
                COALESCE(c.grand_total, 0)::numeric AS debit,
                0::numeric AS credit
            FROM challans c
            LEFT JOIN parties pt ON pt.id = c.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name
                FROM orders o2
                WHERE o2.id::text = ANY(c.order_ids)
                LIMIT 1
            ) o ON TRUE
            LEFT JOIN LATERAL (
                SELECT i2.invoice_no
                FROM invoices i2
                WHERE COALESCE(i2.is_deleted,FALSE)=FALSE
                  AND UPPER(COALESCE(i2.status,'')) NOT IN ('VOID','CANCELLED')
                  AND (
                      i2.challan_id = c.id
                      OR i2.order_ids::text[] && c.order_ids::text[]
                  )
                LIMIT 1
            ) inv ON TRUE
            WHERE COALESCE(c.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(c.status,'')) NOT IN ('CANCELLED','VOID')
              AND inv.invoice_no IS NULL
        """

    rows = _q(f"""
        WITH live_entries AS (
            SELECT
                i.invoice_date::date AS entry_date,
                'INVOICE'::text      AS entry_type,
                i.invoice_no::text   AS ref_no,
                COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS party_name,
                COALESCE(i.grand_total, 0)::numeric AS debit,
                0::numeric AS credit,
                ('Sales Invoice ' || COALESCE(i.invoice_no,'') || ' - ' ||
                 COALESCE(pt.party_name, o.party_name, o.patient_name, '')) AS narration,
                COALESCE(i.created_by, '') AS created_by,
                i.created_at
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name
                FROM orders o2
                WHERE o2.id::text = ANY(i.order_ids)
                LIMIT 1
            ) o ON TRUE
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND i.status NOT IN ('VOID','CANCELLED')
              AND i.invoice_no NOT ILIKE '%%COURIER%%'

            {real_challan_union}

            UNION ALL

            SELECT
                p.payment_date::date AS entry_date,
                COALESCE(p.payment_type, 'PAYMENT')::text AS entry_type,
                COALESCE(NULLIF(p.payment_no,''), p.id::text) AS ref_no,
                party_x.party_name,
                0::numeric AS debit,
                COALESCE(p.amount, 0)::numeric AS credit,
                {_PAY_NARRATION_EXPR} AS narration,
                COALESCE(p.created_by, '') AS created_by,
                p.created_at
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            CROSS JOIN LATERAL (
                SELECT {_PAY_PARTY_EXPR} AS party_name
            ) party_x
            WHERE p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
              AND NOT COALESCE(p.is_deleted,FALSE)
        )
        SELECT
            entry_date::text   AS "Date",
            entry_type         AS "Type",
            ref_no             AS "Ref No",
            ROUND(debit,2)     AS "Dr (₹)",
            ROUND(credit,2)    AS "Cr (₹)",
            narration          AS "Narration",
            created_by         AS "By"
        FROM live_entries
        WHERE party_name = %(party)s
          AND entry_date BETWEEN %(fd)s AND %(td)s
        ORDER BY entry_date ASC, created_at ASC
    """, {"party": chosen, "fd": fd, "td": td})

    # Opening balance (before period)
    op_rows = _q(f"""
        WITH live_entries AS (
            SELECT i.invoice_date::date AS entry_date,
                   COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS party_name,
                   COALESCE(i.grand_total, 0)::numeric AS debit,
                   0::numeric AS credit
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name
                FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
            ) o ON TRUE
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND i.status NOT IN ('VOID','CANCELLED')
              AND i.invoice_no NOT ILIKE '%%COURIER%%'
            {real_challan_union_open}
            UNION ALL
            SELECT p.payment_date::date AS entry_date,
                   party_x.party_name,
                   0::numeric AS debit,
                   COALESCE(p.amount, 0)::numeric AS credit
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            CROSS JOIN LATERAL (SELECT {_PAY_PARTY_EXPR} AS party_name) party_x
            WHERE p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
              AND NOT COALESCE(p.is_deleted,FALSE)
        )
        SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c
        FROM live_entries WHERE party_name=%(party)s AND entry_date < %(fd)s
    """, {"party": chosen, "fd": fd})
    op_dr = float((op_rows[0]["d"] if op_rows else 0) or 0)
    op_cr = float((op_rows[0]["c"] if op_rows else 0) or 0)
    opening = op_dr - op_cr

    if not rows:
        _metrics(("Opening Balance", _fmt(opening)), ("Period Entries", "0"), ("Closing", _fmt(opening)))
        st.info("No entries in this period.")
        return

    df = _df(rows)
    df["Dr (₹)"] = pd.to_numeric(df["Dr (₹)"], errors="coerce").fillna(0)
    df["Cr (₹)"] = pd.to_numeric(df["Cr (₹)"], errors="coerce").fillna(0)
    df["Balance (₹)"] = (df["Dr (₹)"] - df["Cr (₹)"]).cumsum() + opening

    total_dr  = df["Dr (₹)"].sum()
    total_cr  = df["Cr (₹)"].sum()
    closing   = opening + total_dr - total_cr

    _metrics(
        ("Opening",  _fmt(opening)),
        ("Period Dr",_fmt(total_dr)),
        ("Period Cr",_fmt(total_cr)),
        ("Closing",  _fmt(closing)),
    )

    st.markdown(
        f"<div style='background:{'#0a2a1a' if closing <= 0 else '#2a0a0a'};"
        f"border:1px solid {'#22c55e' if closing <= 0 else '#ef4444'};"
        f"border-radius:6px;padding:8px 14px;margin:8px 0'>"
        f"{'✅ Settled' if abs(closing) < 0.01 else ('Cr Balance: ' + _fmt(abs(closing))) if closing < 0 else ('Dr Balance (Receivable): ' + _fmt(closing))}"
        f"</div>", unsafe_allow_html=True)

    _render_register_grid(
        df,
        "pl2",
        select_col="Ref No",
        select_state_key="pl2_doc_sel",
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Dr (₹)","Cr (₹)","Balance (₹)"]},
    )
    _render_ledger_share_actions(
        chosen, df, opening, closing, fd, td, ledger_basis,
        is_supplier=False, key="pl2_share"
    )
    _render_party_ledger_special_reports(chosen, fd, td, key="pl2_prod")
    _ledger_doc_action_drawer(df, chosen, "pl2_doc")
    _export(df, f"Ledger_{chosen}_{fd}_{td}", "pl2_dl")


def _render_supplier_ledger_body(chosen, fd, td, ledger_basis):
    real_pa_union = ""
    real_pa_union_open = ""
    if ledger_basis == "Real Ledger":
        real_pa_union = """
            UNION ALL

            SELECT
                COALESCE(pa.document_date, pa.created_at::date, CURRENT_DATE)::date AS entry_date,
                'SUPPLIER_CHALLAN'::text AS entry_type,
                COALESCE(NULLIF(pa.challan_no,''), 'PA-' || pa.id::text) AS ref_no,
                COALESCE(pa.supplier_name, '') AS party_name,
                0::numeric AS debit,
                COALESCE(pa.purchase_price,0)::numeric * COALESCE(pa.qty,1)::numeric AS credit,
                ('Operational supplier challan ' || COALESCE(NULLIF(pa.challan_no,''), pa.id::text) ||
                 CASE WHEN COALESCE(pa.order_no,'') <> '' THEN ' - ' || pa.order_no ELSE '' END) AS narration,
                '' AS created_by,
                COALESCE(pa.created_at, NOW()) AS created_at
            FROM purchase_acknowledgements pa
            WHERE COALESCE(pa.supplier_name,'') <> ''
              AND COALESCE(NULLIF(pa.invoice_no,''), '') = ''
              AND UPPER(COALESCE(pa.billing_status,'')) NOT IN ('INVOICED','VOIDED','CANCELLED')
        """
        real_pa_union_open = """
            UNION ALL

            SELECT
                COALESCE(pa.document_date, pa.created_at::date, CURRENT_DATE)::date AS entry_date,
                COALESCE(pa.supplier_name, '') AS party_name,
                0::numeric AS debit,
                COALESCE(pa.purchase_price,0)::numeric * COALESCE(pa.qty,1)::numeric AS credit
            FROM purchase_acknowledgements pa
            WHERE COALESCE(pa.supplier_name,'') <> ''
              AND COALESCE(NULLIF(pa.invoice_no,''), '') = ''
              AND UPPER(COALESCE(pa.billing_status,'')) NOT IN ('INVOICED','VOIDED','CANCELLED')
        """

    rows = _q(f"""
        WITH live_entries AS (
            SELECT
                pi.invoice_date::date AS entry_date,
                'PURCHASE_INVOICE'::text AS entry_type,
                pi.invoice_no::text AS ref_no,
                COALESCE(pi.supplier_name,'') AS party_name,
                0::numeric AS debit,
                COALESCE(pi.invoice_total,0)::numeric AS credit,
                ('Purchase Invoice ' || COALESCE(pi.invoice_no,'') ||
                 CASE WHEN COALESCE(pi.supplier_invoice_no,'') <> ''
                      THEN ' - Supplier Inv ' || pi.supplier_invoice_no ELSE '' END) AS narration,
                COALESCE(pi.created_by,'') AS created_by,
                pi.created_at
            FROM purchase_invoices pi
            WHERE NOT COALESCE(pi.is_deleted,FALSE)
              AND UPPER(COALESCE(pi.payment_status,'')) != 'VOIDED'

            {real_pa_union}

            UNION ALL

            SELECT
                p.payment_date::date AS entry_date,
                'SUPPLIER_PAYMENT'::text AS entry_type,
                COALESCE(NULLIF(p.payment_no,''), p.id::text) AS ref_no,
                COALESCE(p.party_name,'') AS party_name,
                COALESCE(p.amount,0)::numeric AS debit,
                0::numeric AS credit,
                COALESCE(NULLIF(p.remarks,''), 'Supplier payment') AS narration,
                COALESCE(p.created_by,'') AS created_by,
                p.created_at
            FROM payments p
            WHERE p.payment_type = 'DISBURSEMENT'
              AND NOT COALESCE(p.is_deleted,FALSE)
        )
        SELECT entry_date::text AS "Date",
               entry_type AS "Type",
               ref_no AS "Ref No",
               ROUND(debit,2) AS "Dr (₹)",
               ROUND(credit,2) AS "Cr (₹)",
               narration AS "Narration",
               created_by AS "By"
        FROM live_entries
        WHERE party_name = %(party)s
          AND entry_date BETWEEN %(fd)s AND %(td)s
        ORDER BY entry_date ASC, created_at ASC
    """, {"party": chosen, "fd": fd, "td": td})

    op_rows = _q(f"""
        WITH live_entries AS (
            SELECT pi.invoice_date::date AS entry_date,
                   COALESCE(pi.supplier_name,'') AS party_name,
                   0::numeric AS debit,
                   COALESCE(pi.invoice_total,0)::numeric AS credit
            FROM purchase_invoices pi
            WHERE NOT COALESCE(pi.is_deleted,FALSE)
              AND UPPER(COALESCE(pi.payment_status,'')) != 'VOIDED'
            {real_pa_union_open}
            UNION ALL
            SELECT p.payment_date::date AS entry_date,
                   COALESCE(p.party_name,'') AS party_name,
                   COALESCE(p.amount,0)::numeric AS debit,
                   0::numeric AS credit
            FROM payments p
            WHERE p.payment_type = 'DISBURSEMENT'
              AND NOT COALESCE(p.is_deleted,FALSE)
        )
        SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c
        FROM live_entries
        WHERE party_name = %(party)s
          AND entry_date < %(fd)s
    """, {"party": chosen, "fd": fd})

    op_dr = float((op_rows[0]["d"] if op_rows else 0) or 0)
    op_cr = float((op_rows[0]["c"] if op_rows else 0) or 0)
    opening = op_dr - op_cr

    if not rows:
        _metrics(("Opening Balance", _fmt(opening)), ("Period Entries", "0"), ("Closing", _fmt(opening)))
        st.info("No entries in this period.")
        return

    df = _df(rows)
    df["Dr (₹)"] = pd.to_numeric(df["Dr (₹)"], errors="coerce").fillna(0)
    df["Cr (₹)"] = pd.to_numeric(df["Cr (₹)"], errors="coerce").fillna(0)
    df["Balance (₹)"] = (df["Dr (₹)"] - df["Cr (₹)"]).cumsum() + opening
    total_dr = df["Dr (₹)"].sum()
    total_cr = df["Cr (₹)"].sum()
    closing = opening + total_dr - total_cr

    _metrics(
        ("Opening", _fmt(opening)),
        ("Paid / Dr", _fmt(total_dr)),
        ("Payable / Cr", _fmt(total_cr)),
        ("Closing", _fmt(closing)),
    )
    st.markdown(
        f"<div style='background:{'#2a0a0a' if closing < -0.01 else '#0a2a1a'};"
        f"border:1px solid {'#ef4444' if closing < -0.01 else '#22c55e'};"
        f"border-radius:6px;padding:8px 14px;margin:8px 0'>"
        f"{'✅ Settled' if abs(closing) < 0.01 else ('Supplier Payable: ' + _fmt(abs(closing))) if closing < 0 else ('Advance with Supplier: ' + _fmt(closing))}"
        f"</div>", unsafe_allow_html=True)

    _render_register_grid(
        df,
        "spl",
        select_col="Ref No",
        select_state_key="spl_doc_sel",
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Dr (₹)","Cr (₹)","Balance (₹)"]},
    )
    _render_ledger_share_actions(
        chosen, df, opening, closing, fd, td, ledger_basis,
        is_supplier=True, key="spl_share"
    )
    _ledger_doc_action_drawer(df, chosen, "spl_doc")
    _export(df, f"Supplier_Ledger_{chosen}_{fd}_{td}", "spl_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 8. DEBTORS REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_debtors_register():
    st.caption("All parties with outstanding receivables -- invoice-wise")
    fd, td    = _date_filter("dr2", "All time")
    party = _party_filter("dr2", "All Parties / Customers", include_patients=True)
    scope_filter = _account_scope_clause("dr2", "o", "i")
    min_bal   = st.number_input("Min outstanding ₹", value=1.0, step=100.0, key="dr2_min")
    party_filter = "AND COALESCE(p.party_name, o.party_name, o.patient_name, '') ILIKE %(pty)s" if party else ""

    rows = _q("""
        SELECT * FROM (
            SELECT
                COALESCE(p.party_name, o.party_name, o.patient_name, '--') AS "Party",
                COALESCE(p.mobile, o.patient_mobile, '') AS "Mobile",
                COALESCE(p.city,'')          AS "City",
                i.invoice_no                 AS "Invoice",
                i.invoice_date::text         AS "Invoice Date",
                ROUND(i.grand_total,2)       AS "Invoice Amt (₹)",
                -- Use allocator-maintained fields -- no manual advance recalculation
                ROUND(COALESCE(i.amount_paid, 0), 2) AS "Paid (₹)",
                -- Use allocator-maintained balance_due -- never recalculate advance here
                ROUND(COALESCE(i.balance_due, i.grand_total), 2) AS "Balance (₹)",
                CASE
                    WHEN i.due_date IS NULL OR i.due_date >= CURRENT_DATE THEN 'Current'
                    WHEN (CURRENT_DATE - i.due_date) <= 30  THEN '1-30 days'
                    WHEN (CURRENT_DATE - i.due_date) <= 60  THEN '31-60 days'
                    WHEN (CURRENT_DATE - i.due_date) <= 90  THEN '61-90 days'
                    ELSE '90+ days'
                END                          AS "Aging"
            FROM invoices i
            LEFT JOIN parties p ON p.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name, o2.patient_mobile, o2.order_type
                FROM orders o2
                WHERE o2.id::text = ANY(i.order_ids)
                LIMIT 1
            ) o ON TRUE
            WHERE COALESCE(i.is_deleted,FALSE) = FALSE
              AND UPPER(COALESCE(i.status,'')) != 'CANCELLED'
              AND i.invoice_date BETWEEN %(fd)s AND %(td)s
              """ + scope_filter + """
              """ + party_filter + """
        ) sub
        WHERE "Balance (₹)" >= %(mb)s
        ORDER BY "Party", "Invoice Date"
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%", "mb": min_bal})

    if not rows:
        st.success("✅ No outstanding debtors!"); return

    df = _df(rows)
    for c in ["Invoice Amt (₹)","Paid (₹)","Balance (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Party summary
    party_sum = df.groupby("Party")["Balance (₹)"].sum().sort_values(ascending=False)
    _metrics(
        ("Parties",   str(len(party_sum))),
        ("Invoices",  str(len(df))),
        ("Total Due", _fmt(df["Balance (₹)"].sum())),
    )

    # Aging buckets
    bucket_order = ["Current","1-30 days","31-60 days","61-90 days","90+ days"]
    buckets = df.groupby("Aging")["Balance (₹)"].sum().reindex(bucket_order, fill_value=0)
    cols = st.columns(5)
    for i, (bk, amt) in enumerate(buckets.items()):
        cols[i].metric(bk, _fmt(amt))

    _render_register_grid(
        df,
        "dr2",
        select_col="Invoice",
        select_state_key="dr2_doc_sel",
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Invoice Amt (₹)","Paid (₹)","Balance (₹)"]},
    )
    ledger_df = df.rename(columns={"Invoice": "Ref No"}).copy()
    ledger_df["Type"] = "INVOICE"
    selected_invoice = st.session_state.get("dr2_doc_sel")
    selected_party = ""
    if selected_invoice and "Ref No" in ledger_df.columns:
        match = ledger_df[ledger_df["Ref No"].astype(str) == str(selected_invoice)]
        if not match.empty:
            selected_party = str(match.iloc[0].get("Party") or "")
    _ledger_doc_action_drawer(ledger_df, selected_party or (party or ""), "dr2_doc")
    _export(df, f"Debtors_Register_{fd}_{td}", "dr2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 9. ORDER REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_order_register():
    st.caption("All orders -- retail, wholesale, consultation")
    fd, td   = _date_filter("or2")
    party    = _party_filter("or2", "All Parties", include_patients=True)
    grouping = _grouping("or2")

    status_opts = ["All","PENDING","CONFIRMED","IN_PRODUCTION","READY","BILLED","DELIVERED","CLOSED","CANCELLED"]
    otype_opts  = ["All","RETAIL","WHOLESALE","CONSULTATION"]
    c1, c2 = st.columns(2)
    status = c1.selectbox("Status", status_opts, key="or2_st")
    otype  = c2.selectbox("Type",   otype_opts,  key="or2_type")

    pf = "AND COALESCE(o.party_name, o.patient_name,'') ILIKE %(pty)s" if party else ""
    sf = "" if status == "All" else "AND o.status = %(st)s"
    tf = "" if otype  == "All" else "AND o.order_type = %(ot)s"
    scope = st.session_state.get("or2_scope", "All")
    scf = ""
    if scope == "Retail":
        scf = "AND UPPER(COALESCE(o.order_type,'')) = 'RETAIL'"
    elif scope == "Online":
        scf = "AND UPPER(COALESCE(o.order_type,'')) = 'ONLINE'"
    elif scope == "Wholesale":
        scf = "AND UPPER(COALESCE(o.order_type,'')) NOT IN ('RETAIL','ONLINE')"

    rows = _q("""
        SELECT
            o.created_at::date::text     AS "Date",
            o.order_no                   AS "Order No",
            o.order_type                 AS "Type",
            COALESCE(o.party_name, o.patient_name,'') AS "Party / Patient",
            o.total_items                AS "Items",
            ROUND(COALESCE(o.total_value,0),2) AS "Value (₹)",
            o.status                     AS "Status"
        FROM orders o
        WHERE o.created_at::date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(o.is_deleted,FALSE) = FALSE
          """+ (pf or "") + """ """+ (sf or "") + """ """+ (tf or "") + """ """+ scf + """
        ORDER BY o.created_at DESC
        LIMIT 1000
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%", "st": status, "ot": otype})

    if not rows:
        st.info("No orders in this period."); return

    df = _df(rows)
    if "Value (₹)" in df.columns:
        df["Value (₹)"] = pd.to_numeric(df["Value (₹)"], errors="coerce").fillna(0)

    _metrics(
        ("Orders",      str(len(df))),
        ("Total Value", _fmt(df["Value (₹)"].sum()) if "Value (₹)" in df.columns else "--"),
    )

    if grouping != "Detail":
        df["_dt"] = pd.to_datetime(df["Date"], errors="coerce")
        if   grouping == "Daily":   df["Period"] = df["_dt"].dt.strftime("%Y-%m-%d")
        elif grouping == "Monthly": df["Period"] = df["_dt"].dt.strftime("%Y-%m")
        else:                       df["Period"] = df["_dt"].dt.year.astype(str)
        display = df.groupby("Period").agg(
            Orders=("Order No","count"),
            **{"Value (₹)": ("Value (₹)","sum")}
        ).reset_index().sort_values("Period")
        st.dataframe(display, width='stretch', hide_index=True,
            column_config={"Value (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    else:
        st.dataframe(df, width='stretch', hide_index=True,
            column_config={"Value (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    _export(df, f"Order_Register_{fd}_{td}", "or2_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 10. JOURNAL REGISTER
# ══════════════════════════════════════════════════════════════════════════════

def render_journal_register():
    st.caption("Accounting voucher register. Manual JV/Contra are entered from 📒 Accounts → ✏️ Journal Entry.")
    fd, td   = _date_filter("jr")
    grouping = _grouping("jr")
    vtype_opts = ["All","JOURNAL","CONTRA","PURCHASE","SALES","RECEIPT","PAYMENT"]
    src_opts   = ["Manual","All","Auto-posted"]
    c1, c2 = st.columns(2)
    vtype  = c1.selectbox("Voucher Type", vtype_opts, key="jr_vtype")
    src    = c2.selectbox(
        "Source",
        src_opts,
        key="jr_src",
        help="Manual shows user-entered JV/Contra/Purchase vouchers. Auto-posted shows system vouchers from invoices, receipts, and payments.",
    )

    tf = "" if vtype == "All" else "AND j.voucher_type = %(vt)s"
    af = "" if src   == "All" else \
         "AND j.is_auto_posted = TRUE" if src == "Auto-posted" else \
         "AND j.is_auto_posted = FALSE"

    rows = _q("""
        SELECT
            j.voucher_date::text    AS "Date",
            j.voucher_no            AS "Voucher No",
            j.voucher_type          AS "Type",
            COALESCE(NULLIF(j.narration,''), j.voucher_type || ' ' || COALESCE(j.ref_doc_no, j.voucher_no, '')) AS "Narration",
            ROUND(j.total_debit,2)  AS "Dr (₹)",
            ROUND(j.total_credit,2) AS "Cr (₹)",
            j.ref_doc_no            AS "Ref Doc",
            j.created_by            AS "By",
            j.is_auto_posted        AS "Auto"
        FROM journal_entries j
        WHERE j.voucher_date BETWEEN %(fd)s AND %(td)s
          """+ (tf or "") +""" """+ (af or "") +"""
        ORDER BY j.voucher_date DESC, j.created_at DESC
        LIMIT 500
    """, {"fd": fd, "td": td, "vt": vtype})

    if not rows:
        st.info("No journal entries. Run Backfill in Accounts to generate."); return

    df = _df(rows)
    for c in ["Dr (₹)","Cr (₹)"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Vouchers",     str(len(df))),
        ("Total Dr",     _fmt(df["Dr (₹)"].sum())),
        ("Total Cr",     _fmt(df["Cr (₹)"].sum())),
    )
    display = _apply_grouping(df, grouping, "Dr (₹)", "Cr (₹)")
    st.dataframe(display, width='stretch', hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in display.select_dtypes("number").columns
                       if c not in ["Auto"]})
    _journal_action_drawer(df, "jr_doc")
    _export(df, f"Journal_Register_{fd}_{td}", "jr_dl")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# 11. SALES SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def render_sales_summary():
    st.caption("Sales, collection, and outstanding -- party-wise summary")
    fd, td = _date_filter("ss")
    party  = _party_filter("ss", "All Parties", include_patients=True)
    pf = "AND COALESCE(pt.party_name, o.party_name, o.patient_name,'') ILIKE %(pty)s" if party else ""

    _ss_params = {"fd": fd, "td": td}
    _ss_party_filter = ""
    if party:
        _ss_party_filter = "AND COALESCE(pt.party_name, o.party_name, o.patient_name,'') ILIKE %(pty)s"
        _ss_params["pty"] = f"%{party or ''}%"
    scope_filter = _account_scope_clause("ss", "o", "i")
    rows = _q("""
        SELECT
            COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS "Party",
            COUNT(DISTINCT i.id)                         AS "Invoices",
            ROUND(SUM(i.grand_total), 2)                 AS "Total Sales (₹)",
            ROUND(SUM(COALESCE(i.amount_paid, 0)), 2)    AS "Collected (₹)",
            ROUND(SUM(COALESCE(i.balance_due, i.grand_total)), 2) AS "Outstanding (₹)",
            SUM(CASE WHEN COALESCE(i.balance_due,i.grand_total) <= 0.50 THEN 1 ELSE 0 END) AS "Paid",
            SUM(CASE WHEN COALESCE(i.balance_due,i.grand_total) >  0.50 THEN 1 ELSE 0 END) AS "Pending"
        FROM invoices i
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2
            WHERE o2.id::text = ANY(i.order_ids)
            LIMIT 1
        ) o ON TRUE
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND i.invoice_no NOT ILIKE '%%COURIER%%'
    """ + (_ss_party_filter and f" {_ss_party_filter}" or "") + f" {scope_filter}" + """
        GROUP BY COALESCE(pt.party_name, o.party_name, o.patient_name, '--')
        ORDER BY "Outstanding (₹)" DESC, "Total Sales (₹)" DESC
    """, _ss_params)

    if not rows:
        st.info("No sales in this period."); return

    df = _df(rows)
    for c in ["Total Sales (₹)", "Collected (₹)", "Outstanding (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Parties",      str(len(df))),
        ("Total Sales",  _fmt(df["Total Sales (₹)"].sum())),
        ("Collected",    _fmt(df["Collected (₹)"].sum())),
        ("Outstanding",  _fmt(df["Outstanding (₹)"].sum())),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Total Sales (₹)", "Collected (₹)", "Outstanding (₹)"]})
    _export(df, f"Sales_Summary_{fd}_{td}", "ss_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 12. PRODUCT-WISE SALES
# ══════════════════════════════════════════════════════════════════════════════

def render_product_sales():
    st.caption("Product sales with party and period breakdown. Discounts are pulled from linked order lines where available.")
    fd, td = _date_filter("ps")
    party = _party_filter("ps", "All Parties", include_patients=True)
    group_by = st.selectbox(
        "View by",
        ["Product", "Party + Product", "Daily + Product", "Monthly + Product"],
        key="ps_group_by",
    )
    party_filter = "AND COALESCE(pt.party_name, o.party_name, o.patient_name, '--') ILIKE %(pty)s" if party else ""
    scope_filter = _account_scope_clause("ps", "o", "i")
    if group_by == "Party + Product":
        dims = """COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS "Party",
                  COALESCE(il.product_name, '--') AS "Product","""
        group_cols = "COALESCE(pt.party_name, o.party_name, o.patient_name, '--'), il.product_name"
        order_cols = '"Party", "Total Sales (₹)" DESC'
    elif group_by == "Daily + Product":
        dims = """i.invoice_date::text AS "Period",
                  COALESCE(il.product_name, '--') AS "Product","""
        group_cols = "i.invoice_date, il.product_name"
        order_cols = '"Period" DESC, "Total Sales (₹)" DESC'
    elif group_by == "Monthly + Product":
        dims = """TO_CHAR(i.invoice_date, 'YYYY-MM') AS "Period",
                  COALESCE(il.product_name, '--') AS "Product","""
        group_cols = "TO_CHAR(i.invoice_date, 'YYYY-MM'), il.product_name"
        order_cols = '"Period" DESC, "Total Sales (₹)" DESC'
    else:
        dims = """COALESCE(il.product_name, '--') AS "Product","""
        group_cols = "il.product_name"
        order_cols = '"Total Sales (₹)" DESC'

    rows = _q(f"""
        SELECT
            {dims}
            COALESCE(SUM(il.quantity), COUNT(*))          AS "Qty Sold",
            ROUND(SUM(il.unit_price * il.quantity), 2)   AS "Taxable (₹)",
            ROUND(SUM(COALESCE(ol.discount_amount, 0)), 2) AS "Discount (₹)",
            ROUND(SUM(COALESCE(il.tax_amount, 0)), 2)    AS "GST (₹)",
            ROUND(SUM(COALESCE(il.line_total,
                               il.total_price, 0)), 2)   AS "Total Sales (₹)"
        FROM invoice_lines il
        JOIN invoices i ON i.id = il.invoice_id
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2
            WHERE o2.id::text = ANY(i.order_ids)
            LIMIT 1
        ) o ON TRUE
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND COALESCE(il.is_deleted, FALSE) = FALSE
          {party_filter}
          {scope_filter}
        GROUP BY {group_cols}
        ORDER BY {order_cols}
        LIMIT 200
    """, {"fd": fd, "td": td, "pty": f"%{party or ''}%"})

    if not rows:
        st.info("No product sales in this period."); return

    df = _df(rows)
    for c in ["Taxable (₹)", "Discount (₹)", "GST (₹)", "Total Sales (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    _metrics(
        ("Products",    str(len(df))),
        ("Total Qty",   str(int(df["Qty Sold"].sum()) if "Qty Sold" in df.columns else 0)),
        ("Taxable",     _fmt(df["Taxable (₹)"].sum())),
        ("Discount",    _fmt(df["Discount (₹)"].sum() if "Discount (₹)" in df.columns else 0)),
        ("GST",         _fmt(df["GST (₹)"].sum())),
        ("Total Sales", _fmt(df["Total Sales (₹)"].sum())),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Taxable (₹)", "Discount (₹)", "GST (₹)", "Total Sales (₹)"]})
    _export(df, f"Product_Sales_{fd}_{td}", "ps_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 13. DAY-WISE BALANCE HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def render_daywise_balance():
    st.caption("Daily movement from invoices and receipts. Works for CRM parties and retail customers.")
    fd, td = _date_filter("dw")
    party  = _party_filter("dw", "All Parties", include_patients=True)
    _dw_params = {"fd": fd, "td": td}
    scope_filter = _account_scope_clause("dw", "o", "i")
    _dw_pf = ""
    if party:
        _dw_pf = "AND party_name ILIKE %(pty)s"
        _dw_params["pty"] = f"%{party or ''}%"
    rows = _q(f"""
        WITH live_entries AS (
            SELECT i.invoice_date::date AS entry_date,
                   COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS party_name,
                   'INVOICE'::text AS entry_type,
                   COALESCE(i.grand_total, 0)::numeric AS debit,
                   0::numeric AS credit
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            LEFT JOIN LATERAL (
                SELECT o2.party_name, o2.patient_name, o2.order_type
                FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
            ) o ON TRUE
            WHERE COALESCE(i.is_deleted, FALSE) = FALSE
              AND i.status NOT IN ('VOID','CANCELLED')
              AND i.invoice_no NOT ILIKE '%%COURIER%%'
              {scope_filter}
            UNION ALL
            SELECT p.payment_date::date AS entry_date,
                   party_x.party_name,
                   COALESCE(p.payment_type, 'PAYMENT')::text AS entry_type,
                   0::numeric AS debit,
                   COALESCE(p.amount, 0)::numeric AS credit
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN challans c ON c.id = p.challan_id
            LEFT JOIN parties ip ON ip.id = i.party_id
            LEFT JOIN parties cp ON cp.id = c.party_id
            CROSS JOIN LATERAL (SELECT {_PAY_PARTY_EXPR} AS party_name) party_x
            WHERE p.payment_type IN ('PAYMENT','RECEIPT','ADVANCE','OPENING')
              AND NOT COALESCE(p.is_deleted,FALSE)
        )
        SELECT
            entry_date::text AS "Date",
            SUM(CASE WHEN entry_type = 'INVOICE' THEN debit ELSE 0 END) AS "Invoice DR (₹)",
            SUM(CASE WHEN entry_type IN ('PAYMENT','RECEIPT') THEN credit ELSE 0 END) AS "Receipt CR (₹)",
            0::numeric AS "CN CR (₹)",
            SUM(CASE WHEN entry_type = 'ADVANCE' THEN credit ELSE 0 END) AS "Advance CR (₹)",
            SUM(debit - credit) AS "Net Movement (₹)"
        FROM live_entries
        WHERE entry_date BETWEEN %(fd)s AND %(td)s
    """ + (_dw_pf and f" {_dw_pf}" or "") + """
        GROUP BY entry_date
        ORDER BY entry_date ASC
    """, _dw_params)

    if not rows:
        st.info("No ledger entries in this period."); return

    df = _df(rows)
    num_cols = ["Invoice DR (₹)", "Receipt CR (₹)", "CN CR (₹)", "Advance CR (₹)", "Net Movement (₹)"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["Closing Balance (₹)"] = df["Net Movement (₹)"].cumsum()

    _metrics(
        ("Days",         str(len(df))),
        ("Total DR",     _fmt(df["Invoice DR (₹)"].sum())),
        ("Total CR",     _fmt(df["Receipt CR (₹)"].sum() + df["CN CR (₹)"].sum() + df["Advance CR (₹)"].sum())),
        ("Closing",      _fmt(df["Closing Balance (₹)"].iloc[-1] if len(df) else 0)),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in num_cols + ["Closing Balance (₹)"]})
    _export(df, f"Daywise_Balance_{fd}_{td}", "dw_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 14. BALANCE CONFIRMATION REPORT
# ══════════════════════════════════════════════════════════════════════════════

def render_balance_confirmation():
    st.caption("Party statement -- for confirmation / audit / sharing with party")
    fd, td   = _date_filter("bc")
    party    = _party_filter("bc", None, include_patients=True)
    if not party:
        st.info("Select a party to generate the confirmation statement.")
        return

    # Opening balance before period
    op_row = _q("""
        SELECT COALESCE(SUM(debit) - SUM(credit), 0)::numeric AS bal
        FROM party_ledger WHERE party_name = %(pn)s AND entry_date < %(fd)s
    """, {"pn": party, "fd": fd})
    opening = float(op_row[0]["bal"] if op_row else 0)

    # Period entries
    entries = _q("""
        SELECT entry_date::text AS "Date", entry_type AS "Type",
               ref_no AS "Reference", narration AS "Narration",
               ROUND(debit,2) AS "DR (₹)", ROUND(credit,2) AS "CR (₹)"
        FROM party_ledger
        WHERE party_name = %(pn)s AND entry_date BETWEEN %(fd)s AND %(td)s
        ORDER BY entry_date ASC, id ASC
    """, {"pn": party, "fd": fd, "td": td})

    df = _df(entries) if entries else pd.DataFrame()
    if not df.empty:
        for c in ["DR (₹)", "CR (₹)"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df["Balance (₹)"] = (df["DR (₹)"] - df["CR (₹)"]).cumsum() + opening
    total_dr  = float(df["DR (₹)"].sum()) if not df.empty else 0
    total_cr  = float(df["CR (₹)"].sum()) if not df.empty else 0
    closing   = opening + total_dr - total_cr
    inv_outstanding = 0.0
    try:
        _inv_os = _q("""
            SELECT COALESCE(SUM(COALESCE(i.balance_due, i.grand_total, 0)),0)::numeric AS bal
            FROM invoices i
            LEFT JOIN parties pt ON pt.id = i.party_id
            LEFT JOIN orders o ON i.order_ids::text LIKE '%%' || o.id::text || '%%'
            WHERE COALESCE(i.is_deleted,FALSE)=FALSE
              AND COALESCE(i.status,'') NOT IN ('VOID','CANCELLED')
              AND COALESCE(i.invoice_no,'') NOT ILIKE '%%COURIER%%'
              AND i.invoice_date <= %(td)s
              AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') = %(pn)s
        """, {"pn": party, "td": td})
        inv_outstanding = float((_inv_os[0].get("bal") if _inv_os else 0) or 0)
    except Exception:
        inv_outstanding = 0.0

    # Header
    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:8px;"
        f"padding:12px 18px;margin:6px 0'>"
        f"<b style='color:#93c5fd;font-size:1rem'>Account Statement -- {party or ''}</b><br>"
        f"<span style='color:#64748b;font-size:0.80rem'>Period: {fd} to {td}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    _metrics(
        ("Opening Balance", _fmt(opening)),
        ("Total Debit (DR)", _fmt(total_dr)),
        ("Total Credit (CR)", _fmt(total_cr)),
        ("Net Ledger Balance", _fmt(closing)),
        ("Open Invoice Balance", _fmt(inv_outstanding)),
    )
    if abs(inv_outstanding - closing) > 0.50:
        _diff = round(inv_outstanding - closing, 2)
        if _diff > 0:
            st.caption(
                f"Open invoices exceed net ledger by {_fmt(_diff)}. "
                "This usually means unapplied credit/excess advance exists in the ledger."
            )
        else:
            st.caption(
                f"Net ledger exceeds open invoices by {_fmt(abs(_diff))}. "
                "Check unlinked payments, credit notes, or ledger adjustments."
            )
    bal_color = "#4ade80" if closing <= 0.50 else "#ef4444"
    bal_label = "✅ Settled" if abs(closing) < 0.01 else f"{'CR Balance' if closing < 0 else 'DR Balance (Receivable)'}: {_fmt(abs(closing))}"
    st.markdown(
        f"<div style='background:{'#052e16' if closing <= 0.50 else '#1a0505'};"
        f"border:1px solid {bal_color};border-radius:6px;padding:8px 14px;margin:6px 0;"
        f"color:{bal_color};font-weight:700'>{bal_label}</div>",
        unsafe_allow_html=True,
    )

    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                           for c in ["DR (₹)", "CR (₹)", "Balance (₹)"]})
        _export(df, f"Statement_{party or ''}_{fd}_{td}", "bc_dl")
    else:
        st.info("No transactions in this period.")


# ══════════════════════════════════════════════════════════════════════════════
# 15. OUTSTANDING / AGING DETAIL
# ══════════════════════════════════════════════════════════════════════════════

def render_outstanding_detail():
    st.caption("Outstanding invoices with age from invoice/due date. Use this to see how many days/months an amount is pending.")
    f1, f2, f3 = st.columns(3)
    min_bal = f1.number_input("Min outstanding ₹", value=1.0, step=100.0, key="od_min")
    age_basis = f2.selectbox("Age from", ["Due Date if present, else Invoice Date", "Invoice Date"], key="od_age_basis")
    aging_filter = f3.selectbox(
        "Aging bucket",
        ["All", "Current", "1-30 days", "31-60 days", "61-90 days", "90+ days"],
        key="od_age_filter",
    )
    party   = _party_filter("od", "All Parties / Customers", include_patients=True)
    pf = "AND COALESCE(pt.party_name, o.party_name, o.patient_name,'') ILIKE %(pty)s" if party else ""

    age_expr = "COALESCE(i.due_date, i.invoice_date)" if age_basis.startswith("Due Date") else "i.invoice_date"
    scope_filter = _account_scope_clause("od", "o", "i")
    _od_params = {"mb": min_bal}
    _od_pf = ""
    if party:
        _od_pf = "AND COALESCE(pt.party_name, o.party_name, o.patient_name,'') ILIKE %(pty)s"
        _od_params["pty"] = f"%{party or ''}%"
    rows = _q(f"""
        SELECT
            COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS "Party",
            i.invoice_no                AS "Invoice No",
            i.invoice_date::text        AS "Invoice Date",
            i.due_date::text            AS "Due Date",
            GREATEST(0, (CURRENT_DATE - ({age_expr})::date))::int AS "Age Days",
            ROUND(GREATEST(0, (CURRENT_DATE - ({age_expr})::date))::numeric / 30.0, 1) AS "Age Months",
            ROUND(i.grand_total, 2)     AS "Invoice Amt (₹)",
            ROUND(COALESCE(i.amount_paid, 0), 2) AS "Paid (₹)",
            ROUND(COALESCE(i.balance_due, i.grand_total), 2) AS "Outstanding (₹)",
            i.payment_status            AS "Status",
            CASE
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) = 0 THEN 'Current'
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 1  AND 30 THEN '1-30 days'
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 31 AND 60 THEN '31-60 days'
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 61 AND 90 THEN '61-90 days'
                ELSE '90+ days'
            END AS "Aging Bucket",
            CASE
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) = 0 THEN 1
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 1  AND 30 THEN 2
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 31 AND 60 THEN 3
                WHEN GREATEST(0, (CURRENT_DATE - ({age_expr})::date)) BETWEEN 61 AND 90 THEN 4
                ELSE 5
            END AS aging_rank
        FROM invoices i
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
        ) o ON TRUE
        WHERE COALESCE(i.balance_due, i.grand_total) >= %(mb)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND i.invoice_no NOT ILIKE '%%COURIER%%'
    """ + (_od_pf and f" {_od_pf}" or "") + f" {scope_filter}" + """
        ORDER BY aging_rank DESC, "Outstanding (₹)" DESC
    """, _od_params)

    if not rows:
        st.success("✅ No outstanding invoices!"); return

    df = _df(rows)
    for c in ["Invoice Amt (₹)", "Paid (₹)", "Outstanding (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    for c in ["Age Days", "Age Months"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    if aging_filter != "All" and "Aging Bucket" in df.columns:
        df = df[df["Aging Bucket"] == aging_filter]
        if df.empty:
            st.success(f"No outstanding invoices in bucket: {aging_filter}")
            return

    bucket_order = ["Current","1-30 days","31-60 days","61-90 days","90+ days"]
    buckets = df.groupby("Aging Bucket")["Outstanding (₹)"].sum().reindex(bucket_order, fill_value=0)
    cols = st.columns(5)
    for i, (bk, amt) in enumerate(buckets.items()):
        cols[i].metric(bk, _fmt(amt))

    _metrics(
        ("Parties",     str(df["Party"].nunique())),
        ("Invoices",    str(len(df))),
        ("Oldest",      f"{int(df['Age Days'].max()) if 'Age Days' in df.columns and len(df) else 0} days"),
        ("Total Outstanding", _fmt(df["Outstanding (₹)"].sum())),
    )
    if "aging_rank" in df.columns:
        df = df.drop(columns=["aging_rank"])
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Invoice Amt (₹)", "Paid (₹)", "Outstanding (₹)"]})
    _export(df, f"Outstanding_{pd.Timestamp.now().date()}", "od_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 16. SALES PATTERN STUDY
# ══════════════════════════════════════════════════════════════════════════════

def render_sales_pattern():
    st.caption("Day-wise, month-wise sales trends, top customers, unpaid ratio")
    fd, td   = _date_filter("sp")
    grouping = _grouping("sp")

    # Main sales data -- from invoices (allocator-maintained amounts)
    rows = _q("""
        SELECT
            i.invoice_date                                             AS _date,
            COALESCE(pt.party_name, o.party_name, o.patient_name,'--') AS party,
            i.grand_total,
            COALESCE(i.amount_paid, 0)                                AS amount_paid,
            COALESCE(i.balance_due, i.grand_total)                    AS balance_due,
            i.payment_status
        FROM invoices i
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN orders  o  ON o.id::text = ANY(i.order_ids)
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND i.invoice_no NOT ILIKE '%%COURIER%%'
        ORDER BY i.invoice_date ASC
    """, {"fd": fd, "td": td})

    if not rows:
        st.info("No sales in this period."); return

    df = _df(rows)
    df["_date"] = pd.to_datetime(df["_date"], errors="coerce")
    for c in ["grand_total","amount_paid","balance_due"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # Period grouping
    if grouping == "Daily":
        df["Period"] = df["_date"].dt.strftime("%Y-%m-%d")
    elif grouping == "Monthly":
        df["Period"] = df["_date"].dt.strftime("%Y-%m")
    else:
        df["Period"] = df["_date"].dt.year.astype(str)

    period_df = df.groupby("Period").agg(
        Invoices=("grand_total","count"),
        **{"Sales (₹)":       ("grand_total","sum")},
        **{"Collected (₹)":   ("amount_paid","sum")},
        **{"Outstanding (₹)": ("balance_due","sum")},
    ).reset_index().sort_values("Period")

    _metrics(
        ("Total Sales",    _fmt(df["grand_total"].sum())),
        ("Collected",      _fmt(df["amount_paid"].sum())),
        ("Outstanding",    _fmt(df["balance_due"].sum())),
        ("Unpaid Ratio",   f"{df[df['balance_due']>0.50]['grand_total'].sum()/df['grand_total'].sum()*100:.1f}%"),
    )

    st.subheader("📈 Period-wise Sales")
    st.dataframe(period_df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Sales (₹)","Collected (₹)","Outstanding (₹)"]})

    st.subheader("🏆 Top 10 Customers")
    top_cust = df.groupby("party").agg(
        **{"Total (₹)":       ("grand_total","sum")},
        **{"Collected (₹)":   ("amount_paid","sum")},
        **{"Outstanding (₹)": ("balance_due","sum")},
        Invoices=("grand_total","count"),
    ).sort_values("Total (₹)", ascending=False).head(10).reset_index()
    top_cust.rename(columns={"party":"Party"}, inplace=True)
    st.dataframe(top_cust, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Total (₹)","Collected (₹)","Outstanding (₹)"]})

    _export(df, f"Sales_Pattern_{fd}_{td}", "sp_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 17. SCHEME-WISE SALES
# ══════════════════════════════════════════════════════════════════════════════

def render_scheme_wise_sales():
    st.caption("Sales grouped by applied scheme/discount source. Use this to see which schemes are actually moving products.")
    fd, td = _date_filter("sws")
    party = _party_filter("sws", "All Parties / Customers", include_patients=True)
    group_by = st.selectbox(
        "View by",
        ["Scheme", "Scheme + Product", "Scheme + Party", "Daily + Scheme"],
        key="sws_group_by",
    )
    scope_filter = _account_scope_clause("sws", "o", "i")
    pf = ""
    params = {"fd": fd, "td": td}
    if party:
        pf = "AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') ILIKE %(pty)s"
        params["pty"] = f"%{party}%"

    scheme_expr = """
        COALESCE(
            NULLIF(ol.lens_params->>'supplier_scheme_name',''),
            NULLIF(ol.lens_params->>'supplier_scheme_rule',''),
            NULLIF(ol.lens_params->>'cart_scheme_name',''),
            NULLIF(ol.lens_params->>'club_offer_name',''),
            NULLIF(ol.discount_rule,''),
            NULLIF(ol.lens_params->>'discount_rule',''),
            'No Scheme / Manual Price'
        )
    """

    if group_by == "Scheme + Product":
        dims = f"""{scheme_expr} AS "Scheme", COALESCE(il.product_name, '--') AS "Product","""
        group_cols = f"{scheme_expr}, il.product_name"
        order_cols = '"Scheme", "Sales (₹)" DESC'
    elif group_by == "Scheme + Party":
        dims = f"""{scheme_expr} AS "Scheme",
                  COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS "Party","""
        group_cols = f"{scheme_expr}, COALESCE(pt.party_name, o.party_name, o.patient_name, '--')"
        order_cols = '"Scheme", "Sales (₹)" DESC'
    elif group_by == "Daily + Scheme":
        dims = f"""i.invoice_date::text AS "Date", {scheme_expr} AS "Scheme","""
        group_cols = f"i.invoice_date, {scheme_expr}"
        order_cols = '"Date" DESC, "Sales (₹)" DESC'
    else:
        dims = f"""{scheme_expr} AS "Scheme","""
        group_cols = scheme_expr
        order_cols = '"Sales (₹)" DESC'

    rows = _q(f"""
        SELECT
            {dims}
            COUNT(DISTINCT i.id) AS "Invoices",
            COALESCE(SUM(il.quantity), 0) AS "Qty",
            ROUND(SUM(COALESCE(il.total_price, il.unit_price * il.quantity, 0)), 2) AS "Base (₹)",
            ROUND(SUM(COALESCE(ol.discount_amount, 0)), 2) AS "Discount (₹)",
            ROUND(SUM(COALESCE(il.tax_amount, 0)), 2) AS "GST (₹)",
            ROUND(SUM(COALESCE(il.line_total, il.total_price, 0)), 2) AS "Sales (₹)"
        FROM invoice_lines il
        JOIN invoices i ON i.id = il.invoice_id
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2
            WHERE o2.id::text = ANY(i.order_ids)
            LIMIT 1
        ) o ON TRUE
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND COALESCE(il.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND i.invoice_no NOT ILIKE '%%COURIER%%'
          {pf}
          {scope_filter}
        GROUP BY {group_cols}
        ORDER BY {order_cols}
        LIMIT 300
    """, params)

    if not rows:
        st.info("No scheme-linked sales found in this period.")
        return

    df = _df(rows)
    for c in ["Base (₹)", "Discount (₹)", "GST (₹)", "Sales (₹)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    _metrics(
        ("Rows", str(len(df))),
        ("Invoices", str(int(df["Invoices"].sum()) if "Invoices" in df.columns else 0)),
        ("Discount", _fmt(df["Discount (₹)"].sum() if "Discount (₹)" in df.columns else 0)),
        ("Sales", _fmt(df["Sales (₹)"].sum() if "Sales (₹)" in df.columns else 0)),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Base (₹)", "Discount (₹)", "GST (₹)", "Sales (₹)"]})
    _export(df, f"Scheme_Wise_Sales_{fd}_{td}", "sws_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 18. MARGIN ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def render_margin_analysis():
    st.caption("Invoice-line margin using linked procurement cost where available. Lines without cost are highlighted separately.")
    fd, td = _date_filter("ma")
    party = _party_filter("ma", "All Parties / Customers", include_patients=True)
    min_margin = st.number_input("Show margin below %", value=999.0, step=1.0, key="ma_min_margin")
    scope_filter = _account_scope_clause("ma", "o", "i")
    pf = ""
    params = {"fd": fd, "td": td, "mp": min_margin}
    if party:
        pf = "AND COALESCE(pt.party_name, o.party_name, o.patient_name, '') ILIKE %(pty)s"
        params["pty"] = f"%{party}%"

    rows = _q(f"""
        SELECT
            i.invoice_no AS "Invoice",
            i.invoice_date::text AS "Date",
            COALESCE(pt.party_name, o.party_name, o.patient_name, '--') AS "Party",
            COALESCE(il.product_name, '--') AS "Product",
            COALESCE(il.eye_side, ol.eye_side, '') AS "Eye",
            COALESCE(il.quantity, ol.billed_qty, ol.quantity, 1) AS "Qty",
            ROUND(COALESCE(il.line_total, il.total_price, 0), 2) AS "Sales (₹)",
            ROUND(COALESCE(pa.total_value, pa.purchase_price * COALESCE(il.quantity, 1), 0), 2) AS "Cost (₹)",
            ROUND(COALESCE(il.line_total, il.total_price, 0)
                  - COALESCE(pa.total_value, pa.purchase_price * COALESCE(il.quantity, 1), 0), 2) AS "Margin (₹)",
            ROUND(
                CASE WHEN COALESCE(il.line_total, il.total_price, 0) > 0
                     THEN ((COALESCE(il.line_total, il.total_price, 0)
                            - COALESCE(pa.total_value, pa.purchase_price * COALESCE(il.quantity, 1), 0))
                           / COALESCE(il.line_total, il.total_price, 0)) * 100
                     ELSE 0 END, 2
            ) AS "Margin %%",
            CASE WHEN pa.id IS NULL THEN 'NO COST LINK' ELSE 'OK' END AS "Cost Status"
        FROM invoice_lines il
        JOIN invoices i ON i.id = il.invoice_id
        LEFT JOIN order_lines ol ON ol.id = il.order_line_id
        LEFT JOIN purchase_acknowledgements pa ON pa.order_line_id = il.order_line_id
        LEFT JOIN parties pt ON pt.id = i.party_id
        LEFT JOIN LATERAL (
            SELECT o2.party_name, o2.patient_name, o2.order_type
            FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
        ) o ON TRUE
        WHERE i.invoice_date BETWEEN %(fd)s AND %(td)s
          AND COALESCE(i.is_deleted, FALSE) = FALSE
          AND COALESCE(il.is_deleted, FALSE) = FALSE
          AND i.status NOT IN ('VOID','CANCELLED')
          AND i.invoice_no NOT ILIKE '%%COURIER%%'
          {pf}
          {scope_filter}
        ORDER BY "Margin %%" ASC, "Date" DESC
        LIMIT 400
    """, params)

    if not rows:
        st.info("No invoice lines in this period.")
        return

    df = _df(rows)
    for c in ["Sales (₹)", "Cost (₹)", "Margin (₹)", "Margin %"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if min_margin < 999:
        df = df[df["Margin %"] <= min_margin]
    if df.empty:
        st.success("No lines under the selected margin threshold.")
        return
    _metrics(
        ("Lines", str(len(df))),
        ("Sales", _fmt(df["Sales (₹)"].sum())),
        ("Cost", _fmt(df["Cost (₹)"].sum())),
        ("Margin", _fmt(df["Margin (₹)"].sum())),
        ("No Cost Link", str(int((df["Cost Status"] == "NO COST LINK").sum()))),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Sales (₹)", "Cost (₹)", "Margin (₹)"]})
    _export(df, f"Margin_Analysis_{fd}_{td}", "ma_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 19. PROCUREMENT SAVINGS AUDIT
# ══════════════════════════════════════════════════════════════════════════════

def render_procurement_savings():
    st.caption("Compares purchase prices against ophthalmic spec procurement discount columns and scheme rules.")
    fd, td = _date_filter("psa")
    supplier = st.text_input("Supplier contains", key="psa_sup").strip()
    pf = "AND pa.supplier_name ILIKE %(sup)s" if supplier else ""
    params = {"fd": fd, "td": td}
    if supplier:
        params["sup"] = f"%{supplier}%"

    rows = _q(f"""
        SELECT
            COALESCE(pa.invoice_date, pa.document_date, pa.acknowledged_at::date)::text AS "Date",
            COALESCE(pa.supplier_name, '--') AS "Supplier",
            COALESCE(pa.invoice_no, pa.challan_no, '--') AS "Doc No",
            COALESCE(pa.order_no, '--') AS "Order",
            COALESCE(pa.product_name, p.product_name, '--') AS "Product",
            COALESCE(pa.eye_side, '') AS "Eye",
            COALESCE(pa.qty, pa.received_qty, 1) AS "Qty",
            ROUND(COALESCE(pa.purchase_price, 0), 2) AS "Actual Rate (₹)",
            ROUND(COALESCE(ols.wlp_per_pair / 2, ols.purchase_rate, 0), 2) AS "Base Rate (₹)",
            COALESCE(ols.scheme_procurement_discount_pct,
                     ols.normal_procurement_discount_pct, 0) AS "Expected Disc %%",
            ROUND(
                COALESCE(ols.wlp_per_pair / 2, ols.purchase_rate, 0)
                * (1 - COALESCE(ols.scheme_procurement_discount_pct,
                                ols.normal_procurement_discount_pct, 0) / 100.0), 2
            ) AS "Expected Rate (₹)",
            ROUND(
                COALESCE(pa.purchase_price, 0)
                - (
                    COALESCE(ols.wlp_per_pair / 2, ols.purchase_rate, 0)
                    * (1 - COALESCE(ols.scheme_procurement_discount_pct,
                                    ols.normal_procurement_discount_pct, 0) / 100.0)
                  ), 2
            ) AS "Variance (₹)"
        FROM purchase_acknowledgements pa
        LEFT JOIN products p ON p.id = COALESCE(pa.our_product_id, pa.product_id)
        LEFT JOIN LATERAL (
            SELECT os.wlp_per_pair, os.purchase_rate,
                   os.normal_procurement_discount_pct,
                   os.scheme_procurement_discount_pct
            FROM ophthalmic_lens_specs os
            WHERE os.product_id = COALESCE(pa.our_product_id, pa.product_id)
            ORDER BY COALESCE(os.is_active, TRUE) DESC, os.updated_at DESC NULLS LAST
            LIMIT 1
        ) ols ON TRUE
        WHERE COALESCE(pa.invoice_date, pa.document_date, pa.acknowledged_at::date)
              BETWEEN %(fd)s AND %(td)s
          AND COALESCE(pa.purchase_price, 0) > 0
          {pf}
        ORDER BY ABS(
            COALESCE(pa.purchase_price, 0)
            - (
                COALESCE(ols.wlp_per_pair / 2, ols.purchase_rate, 0)
                * (1 - COALESCE(ols.scheme_procurement_discount_pct,
                                ols.normal_procurement_discount_pct, 0) / 100.0)
              )
        ) DESC
        LIMIT 400
    """, params)

    if not rows:
        st.info("No procurement lines found for this period.")
        return
    df = _df(rows)
    for c in ["Actual Rate (₹)", "Base Rate (₹)", "Expected Disc %", "Expected Rate (₹)", "Variance (₹)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    issue_df = df[df["Variance (₹)"].abs() > 1.0]
    _metrics(
        ("Lines", str(len(df))),
        ("Variance Lines", str(len(issue_df))),
        ("Total Variance", _fmt(df["Variance (₹)"].sum())),
        ("Max Variance", _fmt(df["Variance (₹)"].abs().max() if len(df) else 0)),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={c: st.column_config.NumberColumn(format="₹%.2f")
                       for c in ["Actual Rate (₹)", "Base Rate (₹)", "Expected Rate (₹)", "Variance (₹)"]})
    _export(df, f"Procurement_Savings_{fd}_{td}", "psa_dl")


# ══════════════════════════════════════════════════════════════════════════════
# 20. ENTITLEMENT LIABILITY
# ══════════════════════════════════════════════════════════════════════════════

def render_entitlement_liability():
    st.caption("Future reward liability: active, consumed, expired and cancelled entitlements.")
    status = st.selectbox("Status", ["ACTIVE", "CONSUMED", "EXPIRED", "CANCELLED", "ALL"], key="el_status")
    party = _party_filter("el", "All Parties / Customers", include_patients=True)
    where = "WHERE TRUE"
    params = {}
    if status != "ALL":
        where += " AND status = %(st)s"
        params["st"] = status
    if party:
        where += " AND party_name ILIKE %(pty)s"
        params["pty"] = f"%{party}%"
    rows = _q(f"""
        SELECT
            party_name AS "Party",
            COALESCE(scheme_name, '') AS "Scheme",
            trigger_product_name AS "Earned From",
            reward_product_name AS "Reward Product",
            reward_qty AS "Qty",
            reward_billing_value AS "Bill At (₹)",
            earned_at::date::text AS "Earned",
            COALESCE(valid_until, valid_to)::text AS "Valid Until",
            GREATEST(0, COALESCE(valid_until, valid_to) - CURRENT_DATE) AS "Days Left",
            status AS "Status",
            consumed_invoice_no AS "Consumed Invoice",
            cancel_reason AS "Cancel Reason"
        FROM scheme_entitlements
        {where}
        ORDER BY
            CASE status WHEN 'ACTIVE' THEN 1 WHEN 'CONSUMED' THEN 2 WHEN 'EXPIRED' THEN 3 ELSE 4 END,
            COALESCE(valid_until, valid_to) ASC NULLS LAST
        LIMIT 500
    """, params)
    if not rows:
        st.info("No entitlements for this filter.")
        return
    df = _df(rows)
    for c in ["Qty", "Bill At (₹)", "Days Left"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    active = df[df["Status"] == "ACTIVE"] if "Status" in df.columns else df.iloc[0:0]
    _metrics(
        ("Total", str(len(df))),
        ("Active", str(len(active))),
        ("Reward Qty Active", str(float(active["Qty"].sum()) if len(active) else 0)),
        ("Nominal Value", _fmt((active["Qty"] * active["Bill At (₹)"]).sum() if len(active) else 0)),
    )
    st.dataframe(df, use_container_width=True, hide_index=True,
        column_config={"Bill At (₹)": st.column_config.NumberColumn(format="₹%.2f")})
    _export(df, f"Entitlement_Liability_{date.today()}", "el_dl")


def render_registers():
    st.markdown("## 📚 Registers")
    st.caption("Day books, account registers, and transaction logs")

    tabs = st.tabs([
        "🧾 Sales Register",
        "🛒 Purchase Register",
        "💵 Receipt Book",
        "💸 Disbursement Book",
        "💰 Cash Book",
        "🏦 Bank Book",
        "👤 Party / Customer Ledger",
        "📥 Debtors Register",
        "📦 Order Register",
        "📋 Journal Register",
        "📊 Sales Summary",
        "🛍️ Product Sales",
        "⚖️ Outstanding Detail",
        "📅 Day-wise Balance",
        "📋 Balance Confirmation",
        "📈 Sales Pattern",
        "🎯 Scheme Sales",
        "📈 Margin Analysis",
        "🛒 Procurement Savings",
        "🎁 Entitlement Liability",
    ])

    with tabs[0]:  render_sales_register()
    with tabs[1]:  render_purchase_register()
    with tabs[2]:  render_payment_receipt_book()
    with tabs[3]:  render_disbursement_book()
    with tabs[4]:  render_cash_book()
    with tabs[5]:  render_bank_book()
    with tabs[6]:  render_party_ledger()
    with tabs[7]:  render_debtors_register()
    with tabs[8]:  render_order_register()
    with tabs[9]:  render_journal_register()
    with tabs[10]: render_sales_summary()
    with tabs[11]: render_product_sales()
    with tabs[12]: render_outstanding_detail()
    with tabs[13]: render_daywise_balance()
    with tabs[14]: render_balance_confirmation()
    with tabs[15]: render_sales_pattern()
    with tabs[16]: render_scheme_wise_sales()
    with tabs[17]: render_margin_analysis()
    with tabs[18]: render_procurement_savings()
    with tabs[19]: render_entitlement_liability()
