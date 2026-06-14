"""
modules/wa_bulk_sender.py
════════════════════════════════════════════════════════════════════════════
Bulk WhatsApp Pipeline Sender.

Place at: WIN54/modules/wa_bulk_sender.py

WHAT IT DOES
────────────
  1. Admin selects which pipeline stage(s) to notify
  2. System loads all orders in those stages with a mobile number
  3. Shows a preview list — admin can deselect individual orders
  4. Admin presses START — links open one by one with 15-second gap
     (avoids WhatsApp rate-limiting / spam detection)
  5. Full log shown: sent / skipped / no-mobile

WHATSAPP API PLAN
──────────────────
  Phase 1 (now)     — wa.me links opened via browser (manual send)
  Phase 2 (soon)    — Official WhatsApp Business API for:
                       - Order ready notification (READY)
                       - Dispatch notification (DISPATCHED)
                       - Invoice notification (BILLED)
  Phase 3 (planned) — Fully automated: no browser, API sends directly
                       Templates pre-approved by Meta

USAGE
──────
  In app.py or admin page:
      from modules.wa_bulk_sender import render_wa_bulk_sender
      render_wa_bulk_sender()
"""

from __future__ import annotations
import streamlit as st
import datetime
import time


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


# ── Station config (mirrors backoffice.py _STATIONS) ─────────────────────────
_WA_STATIONS = [
    {
        "id": "ORDER_RECEIVED", "label": "📥 Order Received",
        "status": "PENDING",    "color": "#3b82f6",
        "msg_fn": "order_received",
    },
    {
        "id": "CONFIRMED",      "label": "✅ Order Confirmed",
        "status": "CONFIRMED",  "color": "#6366f1",
        "msg_fn": "order_confirmed",
    },
    {
        "id": "READY",          "label": "📦 Order Ready",
        "status": "READY",      "color": "#10b981",
        "msg_fn": "order_ready",
    },
    {
        "id": "BILLED",         "label": "🧾 Invoice Generated",
        "status": "BILLED",     "color": "#059669",
        "msg_fn": "order_billed",
    },
    {
        "id": "DISPATCHED",     "label": "🚚 Dispatched",
        "status": "DISPATCHED", "color": "#0891b2",
        "msg_fn": "order_dispatched",
    },
    {
        "id": "DELIVERED",      "label": "✅ Delivered",
        "status": "DELIVERED",  "color": "#166534",
        "msg_fn": "order_delivered",
    },
]

_API_READY_STAGES = {"READY", "DISPATCHED", "BILLED"}   # planned for Phase 2 API


def _build_message(order: dict, stage_id: str, shop_name: str) -> str:
    """Build the WhatsApp message for a given stage."""
    party    = str(order.get("patient_name") or order.get("party_name") or "Customer")
    order_no = str(order.get("display_order_no") or order.get("order_no") or "")
    total    = float(order.get("total_value") or 0)

    msgs = {
        "order_received":   (
            f"Dear {party}, your order *{order_no}* has been received at "
            f"*{shop_name}*. We'll get started soon! 🙏"
        ),
        "order_confirmed":  (
            f"Dear {party}, great news! 🎉\n"
            f"Order *{order_no}* is confirmed and being processed at *{shop_name}*."
        ),
        "order_ready":      (
            f"Dear {party}, your order is *Ready!* 📦\n"
            f"Order *{order_no}* is ready for pickup/delivery at *{shop_name}*.\n"
            f"Please contact us to arrange delivery."
        ),
        "order_billed":     (
            f"Dear {party}, your invoice has been generated. 🧾\n"
            f"Order *{order_no}* — Amount: *₹{total:,.0f}*\n"
            f"Please contact *{shop_name}* for payment."
        ),
        "order_dispatched": (
            f"Dear {party}, your order *{order_no}* is on its way! 🚚\n"
            f"It has been dispatched from *{shop_name}*. "
            f"We will share tracking details shortly."
        ),
        "order_delivered":  (
            f"Dear {party}, order *{order_no}* has been delivered! ✅\n"
            f"Thank you for choosing *{shop_name}*. "
            f"We hope you love your new eyewear! 🙏"
        ),
    }
    fn = next(
        (s["msg_fn"] for s in _WA_STATIONS if s["id"] == stage_id),
        None
    )
    return msgs.get(fn, f"Dear {party}, update on order {order_no} from {shop_name}.")


def _get_orders_for_stage(status: str) -> list[dict]:
    """Load all active orders in a given status with a mobile number."""
    return _q("""
        SELECT
            id::text                                            AS oid,
            COALESCE(display_order_no::text, order_no)         AS order_no,
            COALESCE(patient_name, party_name, '')             AS party_name,
            COALESCE(patient_mobile,
                     (SELECT mobile FROM parties p WHERE p.id = orders.party_id LIMIT 1),
                     '')                                      AS mobile,
            COALESCE(total_value, 0)                           AS total_value,
            status,
            created_at::date::text                             AS date
        FROM orders
        WHERE status = %(s)s
          AND COALESCE(is_deleted, FALSE) = FALSE
          AND order_type NOT IN ('CONSULTATION')
        ORDER BY created_at DESC
    """, {"s": status})


def _clean_mobile(raw: str) -> str:
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _wa_url(mobile: str, msg: str) -> str:
    import urllib.parse
    try:
        from modules.wa_engine import _clean_mobile as _cm
        mob = _cm(mobile)
    except Exception:
        mob = _clean_mobile(mobile)
    if not mob:
        return ""
    return f"https://wa.me/91{mob}?text={urllib.parse.quote(msg)}"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RENDER
# ─────────────────────────────────────────────────────────────────────────────

def render_wa_bulk_sender() -> None:
    """
    Full bulk WhatsApp sender UI.
    Call from admin panel or a dedicated Streamlit page.
    """
    try:
        from modules.settings.shop_master import get_unit_info
        _si = get_unit_info("retail") or {}
        _shop = _si.get("shop_name", "DV Optical")
    except Exception:
        _shop = "DV Optical"

    st.markdown("## 📲 Bulk WhatsApp Sender")
    st.markdown(
        "<div style='background:#0f172a;border-left:3px solid #25D366;"
        "padding:10px 16px;border-radius:6px;margin-bottom:12px'>"
        "<span style='color:#4ade80;font-weight:700'>Phase 1 — Browser Links</span> &nbsp;·&nbsp; "
        "<span style='color:#94a3b8;font-size:0.82rem'>"
        "Opens WhatsApp links one by one with 15-second gap. "
        "You confirm each send manually in your browser.</span><br>"
        "<span style='color:#475569;font-size:0.75rem;margin-top:4px;display:block'>"
        "📋 Phase 2 planned: Official WhatsApp Business API for READY / DISPATCHED / BILLED — "
        "fully automated, no browser required.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Step 1: Select stages ─────────────────────────────────────────────
    st.markdown("### Step 1 — Select pipeline stages to notify")
    cols = st.columns(3)
    selected_stages = []
    for i, s in enumerate(_WA_STATIONS):
        with cols[i % 3]:
            _api_badge = (
                " <span style='background:#7c3aed22;color:#a78bfa;"
                "font-size:0.6rem;padding:1px 5px;border-radius:8px'>API-ready</span>"
                if s["status"] in _API_READY_STAGES else ""
            )
            checked = st.checkbox(
                s["label"],
                key=f"bulk_stage_{s['id']}",
            )
            if checked:
                selected_stages.append(s)

    if not selected_stages:
        st.info("← Select at least one stage to load orders")
        return

    # ── Step 2: Load orders ───────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Step 2 — Preview orders to notify")

    all_items: list[dict] = []
    for s in selected_stages:
        orders = _get_orders_for_stage(s["status"])
        for o in orders:
            mob = _clean_mobile(o.get("mobile",""))
            all_items.append({
                "stage_id":   s["id"],
                "stage_label": s["label"],
                "color":      s["color"],
                "order_no":   o["order_no"],
                "oid":        o["oid"],
                "party_name": o["party_name"],
                "mobile_raw": o.get("mobile",""),
                "mobile":     mob,
                "has_mobile": bool(mob),
                "msg":        _build_message(o, s["id"], _shop),
                "date":       o.get("date",""),
            })

    if not all_items:
        st.warning("No orders found in the selected stages.")
        return

    with_mob  = [x for x in all_items if x["has_mobile"]]
    no_mob    = [x for x in all_items if not x["has_mobile"]]

    st.markdown(
        f"Found **{len(all_items)}** order(s) — "
        f"**{len(with_mob)}** have mobile number, "
        f"**{len(no_mob)}** will be skipped (no mobile)."
    )

    # ── Select/deselect individual orders ─────────────────────────────────
    _sel_key = "bulk_wa_selected"
    if _sel_key not in st.session_state:
        st.session_state[_sel_key] = {x["oid"]: True for x in with_mob}

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Select All", key="bulk_sel_all"):
            for x in with_mob:
                st.session_state[_sel_key][x["oid"]] = True
            st.rerun()
    with c2:
        if st.button("☐ Deselect All", key="bulk_desel_all"):
            for x in with_mob:
                st.session_state[_sel_key][x["oid"]] = False
            st.rerun()

    # Show preview table
    for item in all_items:
        c_chk, c_info, c_prev = st.columns([0.08, 1.8, 2.2])
        with c_chk:
            if item["has_mobile"]:
                _checked = st.checkbox(
                    "",
                    value=st.session_state[_sel_key].get(item["oid"], True),
                    key=f"bulk_chk_{item['oid']}",
                    label_visibility="collapsed",
                )
                st.session_state[_sel_key][item["oid"]] = _checked
            else:
                st.markdown("🚫", unsafe_allow_html=True)
        with c_info:
            _mob_disp = item["mobile"] or "—"
            st.markdown(
                f"<div style='background:#0f172a;border-left:3px solid {item['color']};"
                f"border-radius:4px;padding:5px 10px;margin:2px 0'>"
                f"<span style='color:#e2e8f0;font-size:0.82rem;font-weight:600'>"
                f"{item['order_no']}</span> &nbsp;"
                f"<span style='color:#94a3b8;font-size:0.75rem'>{item['party_name']}</span><br>"
                f"<span style='color:#64748b;font-size:0.7rem'>📱 {_mob_disp} &nbsp;·&nbsp; "
                f"{item['stage_label']} &nbsp;·&nbsp; {item['date']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with c_prev:
            if item["has_mobile"]:
                with st.expander("👁 Preview message", expanded=False):
                    st.caption(item["msg"])

    selected_to_send = [
        x for x in with_mob
        if st.session_state[_sel_key].get(x["oid"], True)
    ]

    # ── Step 3: Interval setting ──────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Step 3 — Sending settings")
    _interval = st.slider(
        "Gap between each message (seconds)",
        min_value=10, max_value=60, value=15, step=5,
        key="bulk_wa_interval",
        help="15 seconds recommended. WhatsApp detects bulk sending — add a gap.",
    )

    # ── Step 4: Start ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f"### Step 4 — Send to {len(selected_to_send)} order(s)")

    _run_key  = "bulk_wa_running"
    _done_key = "bulk_wa_done"
    _idx_key  = "bulk_wa_idx"
    _log_key  = "bulk_wa_log"

    # Running state
    if st.session_state.get(_run_key):
        idx  = st.session_state.get(_idx_key, 0)
        log  = st.session_state.get(_log_key, [])
        items = st.session_state.get("bulk_wa_queue", [])

        if idx >= len(items):
            # Done
            st.session_state[_run_key] = False
            st.session_state[_done_key] = True
            st.rerun()
        else:
            item = items[idx]
            st.markdown(
                f"<div style='background:#0d2818;border:1px solid #10b981;"
                f"border-radius:8px;padding:14px 16px;margin:8px 0'>"
                f"<div style='color:#4ade80;font-weight:700;margin-bottom:6px'>"
                f"📤 Sending {idx+1} of {len(items)}...</div>"
                f"<div style='color:#94a3b8;font-size:0.85rem'>"
                f"{item['order_no']} · {item['party_name']} · 📱{item['mobile']}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # WhatsApp link button
            url = _wa_url(item["mobile"], item["msg"])
            if url:
                st.markdown(
                    f"<a href='{url}' target='_blank' style='"
                    "display:block;background:#25D366;color:white;"
                    "text-align:center;padding:12px;border-radius:8px;"
                    "font-weight:700;font-size:1rem;text-decoration:none'>"
                    f"📲 Open WhatsApp — {item['order_no']}</a>",
                    unsafe_allow_html=True,
                )

            # Progress bar
            _pct = (idx + 1) / len(items)
            st.progress(_pct, text=f"{idx+1}/{len(items)} — next in {_interval}s")

            # Countdown + Next button
            c_wait, c_skip, c_stop = st.columns(3)
            with c_wait:
                if st.button(
                    f"⏩ Next (sent — waiting {_interval}s)",
                    key=f"bulk_next_{idx}",
                    type="primary",
                    use_container_width=True,
                ):
                    log.append({"order_no": item["order_no"], "status": "sent",
                                "mobile": item["mobile"], "time": datetime.datetime.now().strftime("%H:%M:%S")})
                    st.session_state[_log_key] = log
                    st.session_state[_idx_key] = idx + 1
                    time.sleep(min(_interval, 3))   # short sleep before rerun (rest counted in JS)
                    st.rerun()
            with c_skip:
                if st.button("⏭ Skip this order", key=f"bulk_skip_{idx}", use_container_width=True):
                    log.append({"order_no": item["order_no"], "status": "skipped",
                                "mobile": item["mobile"], "time": datetime.datetime.now().strftime("%H:%M:%S")})
                    st.session_state[_log_key] = log
                    st.session_state[_idx_key] = idx + 1
                    st.rerun()
            with c_stop:
                if st.button("⛔ Stop Sending", key=f"bulk_stop_{idx}", use_container_width=True):
                    st.session_state[_run_key] = False
                    st.session_state[_done_key] = True
                    st.rerun()

        return

    # Done state — show log
    if st.session_state.get(_done_key):
        log = st.session_state.get(_log_key, [])
        sent    = [x for x in log if x["status"] == "sent"]
        skipped = [x for x in log if x["status"] == "skipped"]

        st.success(f"✅ Done — {len(sent)} sent, {len(skipped)} skipped")

        if log:
            st.markdown("**Sending log:**")
            for entry in log:
                icon = "✅" if entry["status"] == "sent" else "⏭"
                st.markdown(
                    f"<div style='font-size:0.78rem;color:#94a3b8;padding:2px 0'>"
                    f"{icon} {entry['time']} &nbsp; {entry['order_no']} &nbsp; 📱{entry['mobile']}</div>",
                    unsafe_allow_html=True,
                )

        if no_mob:
            with st.expander(f"🚫 {len(no_mob)} orders skipped — no mobile number"):
                for x in no_mob:
                    st.caption(f"{x['order_no']} · {x['party_name']}")

        if st.button("🔁 Send Another Batch", key="bulk_restart"):
            for k in (_run_key, _done_key, _idx_key, _log_key, "bulk_wa_queue"):
                st.session_state.pop(k, None)
            st.rerun()
        return

    # Start button
    if not selected_to_send:
        st.warning("No orders selected to send.")
        return

    st.markdown(
        f"<div style='background:#1c1107;border:1px solid #f59e0b;"
        f"border-radius:8px;padding:12px 16px;margin-bottom:8px'>"
        f"<div style='color:#fbbf24;font-weight:700'>⚠️ Ready to send {len(selected_to_send)} messages</div>"
        f"<div style='color:#fde68a;font-size:0.8rem'>"
        f"Each link opens WhatsApp in a new tab. Press 'Send' in WhatsApp for each. "
        f"A {_interval}-second gap will be suggested between each order.</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    if st.button(
        f"🚀 Start Sending — {len(selected_to_send)} orders",
        type="primary",
        use_container_width=True,
        key="bulk_start_btn",
    ):
        st.session_state["bulk_wa_queue"] = selected_to_send
        st.session_state[_run_key]  = True
        st.session_state[_done_key] = False
        st.session_state[_idx_key]  = 0
        st.session_state[_log_key]  = []
        st.rerun()

    # No-mobile report
    if no_mob:
        with st.expander(f"🚫 {len(no_mob)} orders will be skipped — no mobile number"):
            for x in no_mob:
                st.caption(f"{x['order_no']} · {x['party_name']}")


# ─────────────────────────────────────────────────────────────────────────────
# API PHASE 2 STUB
# ─────────────────────────────────────────────────────────────────────────────

def send_via_official_api(
    mobile: str,
    template_name: str,
    params: dict,
    api_key: str = "",
    phone_number_id: str = "",
) -> dict:
    """
    STUB — Phase 2: Official WhatsApp Business API sender.

    Currently returns {"status": "stub", "message": "Not configured yet"}.

    When Meta Business Account is ready:
      1. Set api_key = WHATSAPP_API_KEY (from shop master or env)
      2. Set phone_number_id from Meta Business Manager
      3. Pre-approve templates: order_ready, order_dispatched, invoice_generated
      4. Replace stub with actual requests.post() call

    Template params example:
      template_name = "order_ready"
      params = {
          "party_name": "Aastha Gandhi",
          "order_no":   "ORD-0042",
          "shop_name":  "Parakh Eye Care",
      }
    """
    # TODO Phase 2:
    # import requests
    # response = requests.post(
    #     f"https://graph.facebook.com/v18.0/{phone_number_id}/messages",
    #     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    #     json={
    #         "messaging_product": "whatsapp",
    #         "to": f"91{_clean_mobile(mobile)}",
    #         "type": "template",
    #         "template": {
    #             "name": template_name,
    #             "language": {"code": "en"},
    #             "components": [{
    #                 "type": "body",
    #                 "parameters": [{"type": "text", "text": v} for v in params.values()]
    #             }]
    #         }
    #     }
    # )
    # return response.json()

    return {
        "status":  "stub",
        "message": "WhatsApp Business API not configured yet. "
                   "Set api_key and phone_number_id to activate.",
    }
