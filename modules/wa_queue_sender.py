# -*- coding: utf-8 -*-
"""
modules/wa_queue_sender.py
===========================
WhatsApp message queue — semi-automatic sender.

Correct flow:
  Queue → Open WhatsApp Web → Human presses Send → Click "Mark Sent" → Next

Status lifecycle: PENDING → OPENED → SENT | FAILED | SKIPPED
"""
from __future__ import annotations
import uuid
import datetime
import streamlit as st
from typing import List, Dict, Any


def _q(sql: str, params: dict = None) -> List[Dict[str, Any]]:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}")
        return []


def _w(sql: str, params: dict = None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception:
        return False


def _clean_mobile(mob: str) -> str:
    if not mob:
        return ""
    digits = "".join(c for c in str(mob) if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return ""


def _wa_url(mobile: str, message: str) -> str:
    import urllib.parse
    mob = _clean_mobile(mobile) or mobile
    return f"https://wa.me/91{mob}?text={urllib.parse.quote(message)}"


# ── Public API ────────────────────────────────────────────────────────────────

def enqueue_wa_message(
    mobile:       str,
    message_text: str,
    party_name:   str = "",
    order_no:     str = "",
    message_type: str = "CUSTOM",
    deduplicate:  bool = True,
) -> bool:
    """
    Add a message to the WA queue. Table must exist via migration 0033.
    deduplicate=True (default): skips if same order_no + message_type
    already has a PENDING or OPENED entry — prevents accidental repeat queuing.
    """
    mob = _clean_mobile(mobile)
    if not mob:
        return False

    # Duplicate suppression
    if deduplicate and order_no:
        existing = _q("""
            SELECT id FROM wa_message_queue
            WHERE order_no = %(on)s
              AND message_type = %(mt)s
              AND status IN ('PENDING','OPENED')
            LIMIT 1
        """, {"on": order_no, "mt": message_type})
        if existing:
            return False  # already queued, skip silently

    return _w("""
        INSERT INTO wa_message_queue
            (id, mobile, party_name, order_no, message_type, message_text, status)
        VALUES
            (%(id)s::uuid, %(mob)s, %(pn)s, %(on)s, %(mt)s, %(msg)s, 'PENDING')
    """, {
        "id":  str(uuid.uuid4()),
        "mob": mob,
        "pn":  party_name or "",
        "on":  order_no or "",
        "mt":  message_type,
        "msg": message_text,
    })


# ── Main UI ───────────────────────────────────────────────────────────────────

def render_wa_queue() -> None:
    st.markdown("## 📲 WhatsApp Message Queue")
    st.markdown(
        "<div style='background:#0f172a;border-left:3px solid #25D366;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<span style='color:#4ade80;font-weight:700'>Semi-automatic sending</span>"
        " &nbsp;·&nbsp; "
        "<span style='color:#94a3b8;font-size:0.82rem'>"
        "Queue → Open WhatsApp → You press Send → Click Mark Sent → Next message.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    tab_pending, tab_history, tab_compose = st.tabs([
        "📤 Pending Queue",
        "📋 History",
        "✍️ Compose",
    ])

    with tab_pending:
        _render_pending()

    with tab_history:
        _render_history()

    with tab_compose:
        _render_compose()


# ── Pending tab ───────────────────────────────────────────────────────────────

def _render_pending() -> None:
    pending = _q("""
        SELECT id::text, mobile, party_name, order_no,
               message_type, message_text, status,
               queued_at::text AS queued_at
        FROM wa_message_queue
        WHERE status IN ('PENDING','OPENED')
        ORDER BY queued_at ASC
        LIMIT 100
    """)

    if not pending:
        st.success("✅ Queue is empty.")
        return

    # ── Metrics ───────────────────────────────────────────────────────────────
    n_pending = sum(1 for p in pending if p["status"] == "PENDING")
    n_opened  = sum(1 for p in pending if p["status"] == "OPENED")
    m1, m2, m3 = st.columns(3)
    m1.metric("Total",   len(pending))
    m2.metric("Pending", n_pending)
    m3.metric("Opened",  n_opened,
              delta="WA opened — awaiting Mark Sent" if n_opened else None,
              delta_color="off")

    # ── If currently sending — show the send runner ───────────────────────────
    if st.session_state.get("wa_sending"):
        _run_send_flow(pending)
        return

    # ── Select messages ───────────────────────────────────────────────────────
    sc1, sc2 = st.columns(2)
    if sc1.button("☑️ Select All", key="wa_sel_all"):
        for p in pending:
            st.session_state[f"wa_sel_{p['id']}"] = True
        st.rerun()
    if sc2.button("☐ Deselect All", key="wa_desel_all"):
        for p in pending:
            st.session_state[f"wa_sel_{p['id']}"] = False
        st.rerun()

    selected_ids = []
    for msg in pending:
        mid   = msg["id"]
        pname = msg.get("party_name") or "—"
        mob   = msg.get("mobile","")
        order = msg.get("order_no","")
        mtype = msg.get("message_type","")
        text  = msg.get("message_text","")
        stat  = msg.get("status","PENDING")
        qat   = str(msg.get("queued_at",""))[:16]

        stat_badge = (
            "<span style='background:#1e3a5f;color:#93c5fd;"
            f"font-size:0.65rem;padding:1px 6px;border-radius:6px'>{stat}</span>"
            if stat == "OPENED" else ""
        )

        with st.container(border=True):
            cb_col, info_col, act_col = st.columns([0.5, 6, 2])
            with cb_col:
                checked = st.checkbox(
                    "", key=f"wa_sel_{mid}", value=False,
                    label_visibility="collapsed"
                )
                if checked:
                    selected_ids.append(mid)
            with info_col:
                st.markdown(
                    f"<div style='font-size:0.82rem'>"
                    f"<b style='color:#e2e8f0'>{pname}</b>"
                    f"<span style='color:#64748b'> · 📱 {mob}"
                    f"{' · ' + order if order else ''}</span>"
                    f" {stat_badge}</div>"
                    f"<div style='font-size:0.74rem;color:#94a3b8;"
                    f"margin-top:2px'>{text[:120]}"
                    f"{'...' if len(text)>120 else ''}</div>"
                    f"<div style='font-size:0.67rem;color:#475569'>Queued: {qat}</div>",
                    unsafe_allow_html=True,
                )
            with act_col:
                with st.expander("👁️ Full"):
                    st.caption(text)
                if st.button("⏭️ Skip", key=f"wa_skip_{mid}"):
                    _w("UPDATE wa_message_queue SET status='SKIPPED' WHERE id=%(id)s::uuid",
                       {"id": mid})
                    st.rerun()

    # ── Start sending ─────────────────────────────────────────────────────────
    st.markdown("---")
    if not selected_ids:
        st.info("☑️ Select messages above then click Start.")
        return

    st.markdown(
        f"<div style='color:#4ade80;font-size:0.85rem'>"
        f"<b>{len(selected_ids)}</b> message(s) selected</div>",
        unsafe_allow_html=True,
    )

    if st.button(
        f"▶️ Start Sending {len(selected_ids)} Message(s)",
        type="primary", key="wa_start_btn", use_container_width=True
    ):
        sel = [m for m in pending if m["id"] in selected_ids]
        st.session_state["wa_send_queue"]   = sel
        st.session_state["wa_send_idx"]     = 0
        st.session_state["wa_sending"]      = True
        st.session_state["wa_send_results"] = []
        st.rerun()


def _run_send_flow(pending: list) -> None:
    """
    Semi-automatic send runner.
    Opens WA link → waits for human to press Send → human clicks Mark Sent → advances.
    """
    queue   = st.session_state.get("wa_send_queue", [])
    idx     = st.session_state.get("wa_send_idx", 0)
    results = st.session_state.get("wa_send_results", [])

    if idx >= len(queue):
        # All done
        st.session_state["wa_sending"] = False
        sent    = sum(1 for r in results if r.get("status") == "SENT")
        skipped = sum(1 for r in results if r.get("status") == "SKIPPED")
        st.success(f"✅ Done — {sent} sent, {skipped} skipped")
        if st.button("← Back to Queue", key="wa_done_back"):
            st.session_state.pop("wa_send_queue", None)
            st.session_state.pop("wa_send_idx", None)
            st.session_state.pop("wa_send_results", None)
            st.rerun()
        return

    msg    = queue[idx]
    mid    = msg["id"]
    mobile = msg["mobile"]
    pname  = msg.get("party_name","")
    text   = msg.get("message_text","")
    url    = _wa_url(mobile, text)
    operator = st.session_state.get("user_name","staff")

    # Progress
    st.progress((idx) / len(queue),
                text=f"Message {idx+1} of {len(queue)}")

    # ── Step display ──────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
        f"border-radius:8px;padding:14px 18px;margin:8px 0'>"
        f"<div style='color:#93c5fd;font-size:0.78rem;margin-bottom:4px'>"
        f"MESSAGE {idx+1} of {len(queue)}</div>"
        f"<div style='color:#e2e8f0;font-weight:700;font-size:1rem'>"
        f"📱 {pname} &nbsp; <span style='color:#64748b;font-weight:400'>{mobile}</span></div>"
        f"<div style='color:#94a3b8;font-size:0.80rem;margin-top:6px;"
        f"white-space:pre-line'>{text}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Mark as OPENED when this message is shown (not SENT yet)
    _w("""
        UPDATE wa_message_queue
        SET status = 'OPENED'
        WHERE id = %(id)s::uuid AND status = 'PENDING'
    """, {"id": mid})

    # ── Open WA button ────────────────────────────────────────────────────────
    import base64
    html_page = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<style>body{{background:#0f172a;color:#e2e8f0;font-family:sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;
height:100vh;margin:0;text-align:center;padding:20px}}</style>
</head><body>
<div style="font-size:2rem;margin-bottom:8px">📲</div>
<h2 style="color:#25D366;margin:0 0 4px">WhatsApp Opening...</h2>
<p style="color:#94a3b8;margin:0 0 16px">
  <b>{pname}</b> · {mobile}
</p>
<p style="color:#64748b;font-size:0.85rem;max-width:400px;margin:0 0 20px">
  Message is pre-filled. Press <b>Send</b> in WhatsApp, then come back here
  and click <b style="color:#4ade80">Mark Sent</b>.
</p>
<a href="{url}" target="_blank"
   style="background:#25D366;color:white;padding:14px 32px;
          border-radius:10px;text-decoration:none;font-weight:700;font-size:1.1rem">
  Open WhatsApp ↗
</a>
<p style="color:#334155;font-size:0.72rem;margin-top:24px">
  You can close this tab after sending.
</p>
<script>window.open("{url}", "_blank");</script>
</body></html>"""

    b64 = base64.b64encode(html_page.encode()).decode()
    data_url = f"data:text/html;base64,{b64}"

    st.markdown(
        f'<a href="{data_url}" target="_blank" rel="noopener" '
        f'style="display:inline-block;background:#25D366;color:white;'
        f'padding:12px 24px;border-radius:8px;text-decoration:none;'
        f'font-weight:700;font-size:0.95rem;margin:4px 0">'
        f'📱 Open WhatsApp for {pname}</a>',
        unsafe_allow_html=True,
    )

    st.caption("👆 Click above → WhatsApp opens → Press Send → come back here.")

    # ── Action buttons — human must click ─────────────────────────────────────
    st.markdown("#### Did you send the message?")
    a1, a2, a3 = st.columns(3)

    if a1.button("✅ Mark Sent", key=f"wa_mark_sent_{idx}",
                 type="primary", use_container_width=True):
        _w("""
            UPDATE wa_message_queue
            SET status='SENT', sent_at=NOW(), sent_by=%(by)s
            WHERE id=%(id)s::uuid
        """, {"by": operator, "id": mid})
        results.append({"status": "SENT", "id": mid, "name": pname})
        st.session_state["wa_send_results"] = results
        st.session_state["wa_send_idx"] = idx + 1
        st.rerun()

    if a2.button("❌ Failed / No Response", key=f"wa_mark_fail_{idx}",
                 use_container_width=True):
        _w("""
            UPDATE wa_message_queue
            SET status='FAILED', error_note='Marked failed by operator', sent_by=%(by)s
            WHERE id=%(id)s::uuid
        """, {"by": operator, "id": mid})
        results.append({"status": "FAILED", "id": mid, "name": pname})
        st.session_state["wa_send_results"] = results
        st.session_state["wa_send_idx"] = idx + 1
        st.rerun()

    if a3.button("⏭️ Skip", key=f"wa_skip_send_{idx}",
                 use_container_width=True):
        _w("""
            UPDATE wa_message_queue
            SET status='SKIPPED', sent_by=%(by)s
            WHERE id=%(id)s::uuid
        """, {"by": operator, "id": mid})
        results.append({"status": "SKIPPED", "id": mid, "name": pname})
        st.session_state["wa_send_results"] = results
        st.session_state["wa_send_idx"] = idx + 1
        st.rerun()

    # ── Stop button ───────────────────────────────────────────────────────────
    st.markdown("---")
    if st.button("⏹️ Stop Queue", key=f"wa_stop_{idx}"):
        st.session_state["wa_sending"] = False
        st.rerun()

    # ── Results so far ────────────────────────────────────────────────────────
    if results:
        sent_so_far = sum(1 for r in results if r["status"]=="SENT")
        st.caption(f"So far: {sent_so_far} sent · {idx} processed of {len(queue)}")


# ── History tab ───────────────────────────────────────────────────────────────

def _render_history() -> None:
    today = datetime.date.today()
    c1, c2 = st.columns(2)
    fd = c1.date_input("From", value=today - datetime.timedelta(days=7), key="wa_h_fd")
    td = c2.date_input("To",   value=today, key="wa_h_td")

    rows = _q("""
        SELECT party_name, mobile, order_no, message_type,
               status, queued_at::text AS queued_at,
               sent_at::text AS sent_at, sent_by,
               LEFT(message_text, 100) AS preview
        FROM wa_message_queue
        WHERE queued_at::date BETWEEN %(fd)s AND %(td)s
        ORDER BY queued_at DESC
        LIMIT 300
    """, {"fd": fd.isoformat(), "td": td.isoformat()})

    if not rows:
        st.info("No messages in this period.")
        return

    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total",   len(rows))
    m2.metric("Sent",    counts.get("SENT",0))
    m3.metric("Opened",  counts.get("OPENED",0))
    m4.metric("Pending", counts.get("PENDING",0))
    m5.metric("Failed",  counts.get("FAILED",0) + counts.get("SKIPPED",0))

    import pandas as pd
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ── Compose tab ───────────────────────────────────────────────────────────────

def _render_compose() -> None:
    st.markdown("#### ✍️ Add Message to Queue")

    c1, c2 = st.columns(2)
    mobile = c1.text_input("Mobile", placeholder="9876543210", key="wa_c_mob")
    party  = c2.text_input("Party / Patient", key="wa_c_party")
    order  = c1.text_input("Order No (optional)", key="wa_c_order")
    mtype  = c2.selectbox("Type",
                           ["CUSTOM","ORDER_READY","DISPATCH",
                            "PAYMENT_DUE","REMINDER","OTHER"],
                           key="wa_c_type")
    msg = st.text_area("Message", height=120, key="wa_c_msg")
    st.caption(f"{len(msg)} characters")

    if st.button("➕ Add to Queue", type="primary", key="wa_c_add",
                 disabled=not (mobile and msg)):
        mob = _clean_mobile(mobile)
        if not mob:
            st.error("Invalid mobile -- enter 10-digit Indian number.")
        else:
            ok = enqueue_wa_message(mob, msg, party, order, mtype)
            if ok:
                st.success(f"✅ Added to queue for {party or mob}")
                for k in ["wa_c_mob","wa_c_party","wa_c_order","wa_c_msg"]:
                    st.session_state.pop(k, None)
                st.rerun()
            else:
                st.error("Failed to add.")

    st.markdown("---")
    st.markdown("#### ⚡ Bulk Enqueue from Orders")
    st.caption("Auto-add messages for all eligible orders in a stage.")

    bq1, bq2 = st.columns(2)
    bq_type = bq1.selectbox(
        "Message type",
        ["ORDER_READY","DISPATCH","PAYMENT_DUE","REMINDER"],
        key="wa_bq_type",
    )
    bq_days = bq2.number_input(
        "Orders from last N days", value=1, min_value=1, max_value=30,
        key="wa_bq_days"
    )

    if st.button("🔍 Preview eligible orders", key="wa_bq_preview"):
        # Pull orders matching the type
        from_date = (datetime.date.today() - datetime.timedelta(days=int(bq_days))).isoformat()
        if bq_type in ("ORDER_READY", "DISPATCH"):
            eligible = _q("""
                SELECT o.order_no, COALESCE(o.party_name, o.patient_name,'--') AS party,
                       o.mobile, o.status
                FROM orders o
                WHERE o.created_at::date >= %(fd)s
                  AND o.mobile IS NOT NULL
                  AND COALESCE(o.is_deleted, FALSE) = FALSE
                  AND NOT EXISTS (
                      SELECT 1 FROM wa_message_queue wq
                      WHERE wq.order_no = o.order_no
                        AND wq.message_type = %(mt)s
                        AND wq.status IN ('PENDING','OPENED','SENT')
                  )
                ORDER BY o.created_at DESC LIMIT 50
            """, {"fd": from_date, "mt": bq_type})
        else:  # PAYMENT_DUE, REMINDER
            eligible = _q("""
                SELECT o.order_no, COALESCE(o.party_name, o.patient_name,'--') AS party,
                       o.mobile, i.balance_due
                FROM invoices i
                LEFT JOIN LATERAL (
                    SELECT o2.order_no, o2.party_name, o2.patient_name, o2.mobile
                    FROM orders o2 WHERE o2.id::text = ANY(i.order_ids) LIMIT 1
                ) o ON TRUE
                WHERE i.invoice_date::date >= %(fd)s
                  AND COALESCE(i.balance_due, 0) > 0.50
                  AND o.mobile IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM wa_message_queue wq
                      WHERE wq.order_no = o.order_no
                        AND wq.message_type = %(mt)s
                        AND wq.status IN ('PENDING','OPENED')
                  )
                ORDER BY i.invoice_date DESC LIMIT 50
            """, {"fd": from_date, "mt": bq_type})

        st.session_state["wa_bq_eligible"] = eligible or []
        st.rerun()

    eligible = st.session_state.get("wa_bq_eligible", [])
    if eligible:
        st.markdown(f"**{len(eligible)} order(s) eligible** (not yet queued for {bq_type}):")
        import pandas as pd
        st.dataframe(pd.DataFrame(eligible), use_container_width=True, hide_index=True)

        if st.button(f"➕ Add all {len(eligible)} to Queue",
                     key="wa_bq_add_all", type="primary"):
            added = 0
            for e in eligible:
                mob = _clean_mobile(str(e.get("mobile","") or ""))
                pname = e.get("party","")
                on = e.get("order_no","")
                bal = e.get("balance_due","")
                if not mob:
                    continue
                # Build message based on type
                if bq_type == "ORDER_READY":
                    text = f"Dear {pname}, your order {on} is ready for pickup. Please visit us. -- DV Optical"
                elif bq_type == "DISPATCH":
                    text = f"Dear {pname}, your order {on} has been dispatched. -- DV Optical"
                elif bq_type == "PAYMENT_DUE":
                    text = f"Dear {pname}, a balance of Rs {bal} is due on order {on}. Kindly clear at your earliest. -- DV Optical"
                else:
                    text = f"Dear {pname}, this is a reminder regarding your order {on}. Please contact us. -- DV Optical"
                if enqueue_wa_message(mob, text, pname, on, bq_type, deduplicate=True):
                    added += 1
            st.success(f"✅ {added} message(s) added to queue.")
            st.session_state.pop("wa_bq_eligible", None)
            st.rerun()
