"""
DV ERP — Payment Link Manager
==============================
Generates shareable payment links for retail orders where customer
has not paid advance at punch time, or placed order on hold.

Features
--------
• Generate token-secured payment link per order
• WhatsApp deep-link with pre-filled message
• Customer-facing payment page (no login needed)
• Staff confirmation flow in backoffice
• Order on-hold management
• UPI QR code display on payment page
• Link expiry (configurable, default 72 h)

Flow
----
  Backoffice (Billing Summary)
    → render_payment_link_panel(order, all_lines)
        → create_payment_link(...)
        → WhatsApp button  ──► Customer receives link
                                    ↓
                            render_payment_page(token)  [no auth]
                                    ↓
                            Customer enters UPI ref + clicks "I've Paid"
                                    ↓
                            status → CUSTOMER_CLAIMED
                                    ↓
  Backoffice sees badge "Customer Claims Paid — verify"
    → Staff confirms → payment recorded → order moves forward
"""

from __future__ import annotations
import os, random, string, datetime, urllib.parse
from typing import Optional, List

import streamlit as st

# ── DB helpers ────────────────────────────────────────────────────────────
def _q(sql: str, params: dict | None = None) -> list:
    from modules.sql_adapter import run_query
    return run_query(sql, params) or []

def _fc(v) -> str:
    try:    return f"₹{float(v):,.2f}"
    except: return "₹0.00"

def _fd(v) -> str:
    if not v: return "—"
    try:
        if hasattr(v, "strftime"):
            return v.strftime("%d %b %Y")
        return str(v)[:10]
    except: return str(v)

# ── Style constants ───────────────────────────────────────────────────────
_CARD = "background:#1e293b;border-radius:10px;padding:12px 16px"
_HDR  = "color:#64748b;font-size:0.68rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase"
_VAL  = "font-size:1.1rem;font-weight:700;color:#f1f5f9;margin-top:2px"

# ═══════════════════════════════════════════════════════════════════════════
# SETTINGS HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "") -> str:
    rows = _q("SELECT value FROM system_settings WHERE key=%(k)s LIMIT 1", {"k": key})
    return str((rows[0]["value"] if rows else None) or default)

def set_setting(key: str, value: str) -> None:
    _q("""
        INSERT INTO system_settings (key, value, updated_at)
        VALUES (%(k)s, %(v)s, NOW())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, {"k": key, "v": value})

def get_base_url() -> str:
    """Returns the public app URL used in payment links."""
    env_url = os.environ.get("DV_ERP_BASE_URL", "")
    if env_url:
        return env_url.rstrip("/")
    return get_setting("app_base_url", "http://localhost:8501").rstrip("/")

def get_shop_name() -> str:
    return get_setting("shop_name", "DV Optics")

def get_upi_id() -> str:
    return get_setting("shop_upi_id", "")

def get_upi_name() -> str:
    return get_setting("shop_upi_name", get_shop_name())

# ═══════════════════════════════════════════════════════════════════════════
# LINK CORE
# ═══════════════════════════════════════════════════════════════════════════

def _gen_token(length: int = 12) -> str:
    """12-char URL-safe alphanumeric token."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.SystemRandom().choice(chars) for _ in range(length))

def create_payment_link(
    order_id:   str,
    order_no:   str,
    party_name: str,
    mobile:     str,
    amount:     float,
    description: str = "",
    expiry_hours: int = 72,
    created_by: str = "system",
) -> dict:
    """
    Creates a payment_links row and returns:
    {token, url, whatsapp_url, expires_at}
    """
    # Void any existing PENDING/LINK_SENT links for same order
    _q("""
        UPDATE payment_links
        SET status='VOIDED', void_reason='Replaced by new link', updated_at=NOW()
        WHERE order_id::text=%(oid)s
          AND status IN ('PENDING','LINK_SENT','CUSTOMER_CLAIMED')
    """, {"oid": str(order_id)})

    token      = _gen_token()
    expires_at = datetime.datetime.now() + datetime.timedelta(hours=expiry_hours)

    _q("""
        INSERT INTO payment_links
            (token, order_id, order_no, party_name, mobile,
             amount, description, status, expires_at, created_by, created_at)
        VALUES
            (%(t)s, %(oid)s::uuid, %(ono)s, %(pn)s, %(mob)s,
             %(amt)s, %(desc)s, 'PENDING', %(exp)s, %(by)s, NOW())
    """, {
        "t":    token,
        "oid":  str(order_id),
        "ono":  str(order_no),
        "pn":   str(party_name),
        "mob":  str(mobile or ""),
        "amt":  float(amount),
        "desc": description or f"Order {order_no}",
        "exp":  expires_at,
        "by":   created_by,
    })

    url = f"{get_base_url()}/?pay={token}"
    whatsapp_url = _build_whatsapp_url(mobile, party_name, order_no, amount, url)

    return {
        "token":         token,
        "url":           url,
        "whatsapp_url":  whatsapp_url,
        "expires_at":    expires_at,
    }

def _build_whatsapp_url(mobile: str, name: str,
                         order_no: str, amount: float, link: str) -> str:
    shop = get_shop_name()
    clean_mobile = "".join(c for c in (mobile or "") if c.isdigit())
    if clean_mobile and not clean_mobile.startswith("91") and len(clean_mobile) == 10:
        clean_mobile = "91" + clean_mobile

    msg = (
        f"Dear {name},\n\n"
        f"Thank you for your order at *{shop}*.\n\n"
        f"*Order:* {order_no}\n"
        f"*Amount Due:* ₹{amount:,.2f}\n\n"
        f"Please complete your payment using the link below:\n"
        f"{link}\n\n"
        f"_Link valid for 72 hours_\n"
        f"— {shop}"
    )

    base = f"https://wa.me/{clean_mobile}" if clean_mobile else "https://wa.me/"
    return f"{base}?text={urllib.parse.quote(msg)}"

def get_active_link(order_id: str) -> Optional[dict]:
    rows = _q("""
        SELECT * FROM payment_links
        WHERE order_id::text=%(oid)s
          AND status IN ('PENDING','LINK_SENT','CUSTOMER_CLAIMED')
          AND expires_at > NOW()
        ORDER BY created_at DESC LIMIT 1
    """, {"oid": str(order_id)})
    return rows[0] if rows else None

def get_link_by_token(token: str) -> Optional[dict]:
    rows = _q("SELECT * FROM payment_links WHERE token=%(t)s LIMIT 1", {"t": token})
    return rows[0] if rows else None

def mark_link_sent(token: str) -> None:
    _q("""UPDATE payment_links
         SET status='LINK_SENT', updated_at=NOW()
         WHERE token=%(t)s AND status='PENDING'""", {"t": token})

def customer_claim_payment(token: str, ref: str, method: str, note: str = "") -> bool:
    """Customer submits 'I've paid' on payment page."""
    rows = _q("SELECT * FROM payment_links WHERE token=%(t)s LIMIT 1", {"t": token})
    if not rows:
        return False
    lnk = rows[0]
    if str(lnk.get("status") or "") in ("PAID", "VOIDED", "EXPIRED"):
        return False
    _q("""
        UPDATE payment_links
        SET status='CUSTOMER_CLAIMED',
            customer_ref=%(r)s, customer_method=%(m)s, customer_note=%(n)s,
            updated_at=NOW()
        WHERE token=%(t)s
    """, {"t": token, "r": ref, "m": method, "n": note})
    return True

def confirm_payment(token: str, confirmed_by: str) -> Optional[dict]:
    """
    Staff confirms customer's claim → records in payments table.
    Returns the payment_link row on success.
    """
    rows = _q("SELECT * FROM payment_links WHERE token=%(t)s LIMIT 1", {"t": token})
    if not rows:
        return None
    lnk = rows[0]

    from modules.billing.payment_manager import _submit_payment
    try:
        pid = _submit_payment(
            order_id     = str(lnk["order_id"]),
            party_id     = None,
            party_name   = str(lnk.get("party_name") or ""),
            amount       = float(lnk["amount"]),
            method       = str(lnk.get("customer_method") or "UPI"),
            pay_date     = datetime.date.today(),
            ref_no       = str(lnk.get("customer_ref") or ""),
            remarks      = f"Payment link {token} — confirmed by {confirmed_by}",
            payment_type = "ADVANCE",
            challan_id   = None,
            invoice_id   = None,
        )
        _q("""
            UPDATE payment_links
            SET status='PAID', payment_id=%(pid)s::uuid,
                paid_at=NOW(), paid_amount=%(amt)s, updated_at=NOW()
            WHERE token=%(t)s
        """, {"t": token, "pid": str(pid) if pid else None,
               "amt": float(lnk["amount"])})
        # Release order hold if it was on hold
        _q("""
            UPDATE orders SET status='CONFIRMED', updated_at=NOW()
            WHERE id::text=%(oid)s AND status='PAYMENT_PENDING'
        """, {"oid": str(lnk["order_id"])})
        return lnk
    except Exception as e:
        raise RuntimeError(f"Payment confirmation failed: {e}") from e

def void_link(token: str, reason: str) -> None:
    _q("""UPDATE payment_links
         SET status='VOIDED', void_reason=%(r)s, updated_at=NOW()
         WHERE token=%(t)s""", {"t": token, "r": reason})

def expire_stale_links() -> int:
    """Mark expired links. Call periodically."""
    rows = _q("""
        UPDATE payment_links SET status='EXPIRED', updated_at=NOW()
        WHERE status IN ('PENDING','LINK_SENT') AND expires_at < NOW()
        RETURNING id
    """)
    return len(rows)

# ═══════════════════════════════════════════════════════════════════════════
# ORDER ON HOLD
# ═══════════════════════════════════════════════════════════════════════════

def put_order_on_hold(order_id: str, order_no: str, reason: str = "") -> None:
    _q("""
        UPDATE orders SET status='PAYMENT_PENDING',
               notes=COALESCE(notes,'') || %(note)s, updated_at=NOW()
        WHERE id::text=%(oid)s OR order_no=%(ono)s
    """, {
        "oid":  str(order_id),
        "ono":  str(order_no),
        "note": f" [HOLD: {reason}]" if reason else " [HOLD: awaiting payment]",
    })

def release_order_hold(order_id: str, order_no: str) -> None:
    _q("""
        UPDATE orders SET status='CONFIRMED', updated_at=NOW()
        WHERE (id::text=%(oid)s OR order_no=%(ono)s)
          AND status='PAYMENT_PENDING'
    """, {"oid": str(order_id), "ono": str(order_no)})

# ═══════════════════════════════════════════════════════════════════════════
# BACKOFFICE UI — PAYMENT LINK PANEL
# ═══════════════════════════════════════════════════════════════════════════

def render_payment_link_panel(order: dict, all_lines: list):
    """
    Clean ERP payment panel — 3 sections only:
    1. Balance + Generate Link (action-focused)
    2. Active link card (if exists) — with claim confirm/reject
    3. Document history (collapsed)
    """
    import streamlit as st
    order_id   = str(order.get("id") or "")
    order_no   = str(order.get("order_no") or "")
    party_name = str(order.get("party_name") or order.get("patient_name") or "")
    order_type = str(order.get("order_type") or "RETAIL").upper()
    status     = str(order.get("status") or "PENDING").upper()

    if not order_id or len(order_id) < 10:
        return

    # Mobile from customer master — not manual entry
    mobile = str(order.get("patient_mobile") or "")
    if not mobile and order.get("party_id"):
        _pm = _q("SELECT COALESCE(mobile,'') AS mob FROM parties WHERE id::text=%(id)s LIMIT 1",
                 {"id": str(order["party_id"])})
        mobile = str((_pm[0].get("mob") if _pm else None) or "")

    # Expire stale links
    try: expire_stale_links()
    except: pass

    # ── Compute balance ───────────────────────────────────────────────────
    from modules.billing.payment_manager import _compute_order_total, _q as _pmq
    total = _compute_order_total(all_lines, order_type, order_id)
    _all_paid = _pmq("""
        SELECT COALESCE(SUM(p.amount), 0) AS tot
        FROM payments p
        WHERE COALESCE(p.is_deleted, FALSE) = FALSE
          AND (
              p.advance_for_order_id::text = %(oid)s
              OR p.challan_id IN (
                  SELECT c.id FROM challans c
                  WHERE %(oid)s = ANY(c.order_ids::text[])
                    AND c.status NOT IN ('CANCELLED','VOID')
              )
              OR p.invoice_id IN (
                  SELECT i.id FROM invoices i
                  WHERE %(oid)s = ANY(i.order_ids::text[])
                    AND i.status NOT IN ('CANCELLED','VOID')
              )
          )
    """, {"oid": order_id})
    already_paid = float((_all_paid[0]["tot"] if _all_paid else 0) or 0)
    balance = round(max(total - already_paid, 0), 2)

    st.markdown("---")
    st.markdown("### 💰 Payment")

    # ── Section 1: Balance metric + generate link ─────────────────────────
    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("Order Total", f"₹{total:,.2f}")
    _m2.metric("Received", f"₹{already_paid:,.2f}")
    _m3.metric("Balance Due", f"₹{balance:,.2f}",
               delta=f"-₹{balance:,.2f}" if balance > 0 else "✅ Paid",
               delta_color="inverse" if balance > 0 else "normal")

    # ── Section 2: Active link or generate ───────────────────────────────
    active = get_active_link(order_id)

    if active:
        token  = str(active["token"])
        lstatus = str(active.get("status") or "PENDING").upper()
        lamount = float(active.get("amount") or 0)
        url    = f"{get_base_url()}/?pay={token}"
        wa_url = _build_whatsapp_url(
            str(active.get("mobile") or mobile),
            party_name, order_no, lamount, url
        )
        _ST_COLOR = {
            "PENDING":"#f59e0b","LINK_SENT":"#3b82f6",
            "CUSTOMER_CLAIMED":"#a855f7","PAID":"#10b981",
        }
        sc = _ST_COLOR.get(lstatus, "#64748b")
        _ST_LABEL = {
            "PENDING":          "⏳ Link Generated",
            "LINK_SENT":        "📤 Link Sent — Awaiting Payment",
            "CUSTOMER_CLAIMED": "🔔 Customer Claims Paid — Verify!",
        }
        label = _ST_LABEL.get(lstatus, lstatus)

        st.markdown(
            f"<div style='background:#0f172a;border:1px solid {sc}55;"
            f"border-radius:8px;padding:10px 14px;margin:8px 0;"
            f"border-left:4px solid {sc}'>"
            f"<div style='color:{sc};font-weight:700'>{label}</div>"
            f"<div style='color:#64748b;font-size:0.75rem;margin-top:4px'>"
            f"₹{lamount:,.2f} · {active.get('mobile','—')} · "
            f"Expires {_fd(active.get('expires_at'))}</div>"
            f"<div style='color:#475569;font-size:0.7rem;margin-top:4px;"
            f"font-family:monospace;word-break:break-all'>{url}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

        # CUSTOMER CLAIMED — confirm or reject
        if lstatus == "CUSTOMER_CLAIMED":
            ref    = str(active.get("customer_ref") or "")
            method = str(active.get("customer_method") or "UPI")
            st.warning(f"🔔 Customer reports payment via {method} — Ref: `{ref}`")
            _cc1, _cc2 = st.columns(2)
            with _cc1:
                if st.button("✅ Confirm Payment", type="primary",
                             key=f"plm_confirm_{token}", use_container_width=True):
                    try:
                        from modules.security.roles import current_user_name
                        _by = current_user_name()
                        if not isinstance(_by, str): _by = getattr(_by,"name","staff")
                    except: _by = "staff"
                    confirm_payment(token, _by)
                    st.success("✅ Payment confirmed and recorded!")
                    st.rerun()
            with _cc2:
                if st.button("❌ Reject", key=f"plm_reject_{token}",
                             use_container_width=True):
                    void_link(token, "Claim rejected")
                    st.rerun()
        else:
            _la1, _la2, _la3 = st.columns(3)
            with _la1:
                st.link_button("💬 Send WhatsApp", wa_url, use_container_width=True)
            with _la2:
                if st.button("🔄 New Link", key=f"plm_new_{token}",
                             use_container_width=True):
                    void_link(token, "Replaced")
                    st.rerun()
            with _la3:
                if st.button("🗑 Void", key=f"plm_void_{token}",
                             use_container_width=True):
                    void_link(token, "Voided")
                    st.rerun()
    else:
        # No active link — show generate form
        if balance > 0.01:
            _ga1, _ga2 = st.columns([2, 1])
            with _ga1:
                lnk_amount = st.number_input(
                    "Amount ₹", min_value=0.01,
                    value=float(balance), step=1.0,
                    format="%.2f", key=f"plm_amt_{order_id[:8]}"
                )
            with _ga2:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("🔗 Generate Link",
                             type="primary", use_container_width=True,
                             key=f"plm_gen_{order_id[:8]}"):
                    if not mobile or len(mobile.strip()) < 10:
                        st.error("❌ No mobile number for this customer. Add in CRM.")
                    elif lnk_amount > balance + 0.01:
                        st.error(f"❌ Amount ₹{lnk_amount:,.2f} exceeds balance ₹{balance:,.2f}")
                    else:
                        try:
                            from modules.security.roles import current_user_name
                            _by = current_user_name()
                            if not isinstance(_by, str): _by = getattr(_by,"name","staff")
                        except: _by = "staff"
                        result = create_payment_link(
                            order_id     = order_id,
                            order_no     = order_no,
                            party_name   = party_name,
                            mobile       = mobile.strip(),
                            amount       = float(lnk_amount),
                            description  = f"Order {order_no}",
                            expiry_hours = 72,
                            created_by   = _by,
                        )
                        mark_link_sent(result["token"])
                        st.success(f"✅ Link generated!")
                        st.link_button("💬 Send via WhatsApp", result["whatsapp_url"],
                                       use_container_width=True)
                        st.rerun()
        else:
            st.success("✅ Fully paid — no payment link needed.")

        # Paid link record
        paid = _q("""
            SELECT token, paid_at, paid_amount FROM payment_links
            WHERE order_id::text=%(oid)s AND status='PAID'
            ORDER BY paid_at DESC LIMIT 1
        """, {"oid": order_id})
        if paid:
            p = paid[0]
            st.success(f"✅ Paid on {_fd(p.get('paid_at'))} — {_fc(p.get('paid_amount'))}")

    # ── Order hold (admin/manager action — in expander, not main screen) ──
    with st.expander("⚠️ Order Hold / Release", expanded=False):
        _render_hold_controls(order, order_id, order_no, status)

    # ── Document history (collapsed — not main screen) ────────────────────
    with st.expander("📄 Payment Link History", expanded=False):
        history = _q("""
            SELECT token, status, amount, mobile, created_at, paid_at
            FROM payment_links
            WHERE order_id::text=%(oid)s
            ORDER BY created_at DESC
        """, {"oid": order_id})
        if not history:
            st.caption("No links yet.")
        else:
            _ST = {"PENDING":"#f59e0b","LINK_SENT":"#3b82f6",
                   "CUSTOMER_CLAIMED":"#a855f7","PAID":"#10b981",
                   "EXPIRED":"#64748b","VOIDED":"#ef4444"}
            for h in history:
                sc = _ST.get(str(h.get("status") or ""), "#64748b")
                st.markdown(
                    f"<div style='padding:4px 10px;border-left:3px solid {sc};"
                    f"margin:2px 0;font-size:0.75rem'>"
                    f"<code>{h['token']}</code> "
                    f"<span style='color:{sc}'>{h.get('status')}</span> "
                    f"<b>{_fc(h.get('amount'))}</b> · "
                    f"<span style='color:#475569'>{_fd(h.get('created_at'))}</span>"
                    + (f" · ✅ Paid {_fd(h.get('paid_at'))}" if h.get('paid_at') else "")
                    + "</div>",
                    unsafe_allow_html=True
                )


def _render_active_link(active: dict, order: dict,
                         order_id, order_no, party_name, mobile):
    """Renders the active link card."""
    token  = str(active["token"])
    status = str(active.get("status") or "PENDING").upper()
    amount = float(active.get("amount") or 0)
    url    = f"{get_base_url()}/?pay={token}"
    wa_url = _build_whatsapp_url(
        str(active.get("mobile") or mobile),
        party_name, order_no, amount, url
    )

    _ST_COLOR = {
        "PENDING":"#f59e0b","LINK_SENT":"#3b82f6",
        "CUSTOMER_CLAIMED":"#a855f7","PAID":"#10b981",
    }
    sc = _ST_COLOR.get(status, "#64748b")
    _ST_LABEL = {
        "PENDING":           "⏳ Link Generated — Not Yet Sent",
        "LINK_SENT":         "📤 Link Sent — Awaiting Payment",
        "CUSTOMER_CLAIMED":  "🔔 Customer Claims Paid — Verify!",
    }
    label = _ST_LABEL.get(status, status)

    # Banner
    st.markdown(f"""
    <div style='background:#0f172a;border:1px solid {sc}55;border-radius:10px;
                padding:14px 18px;margin:0 0 12px;border-left:4px solid {sc}'>
      <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px'>
        <span style='color:{sc};font-weight:700;font-size:0.9rem'>{label}</span>
        <span style='color:#64748b;font-size:0.72rem'>
          Token: <code style='background:#1e293b;padding:1px 6px;border-radius:4px;
          color:#94a3b8'>{token}</code>
        </span>
      </div>
      <div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px'>
        <div><div style='{_HDR}'>Amount</div><div style='{_VAL};color:{sc}'>{_fc(amount)}</div></div>
        <div><div style='{_HDR}'>Expires</div>
             <div style='{_VAL};font-size:0.78rem'>{_fd(active.get("expires_at"))}</div></div>
        <div><div style='{_HDR}'>Mobile</div>
             <div style='{_VAL};font-size:0.78rem'>{active.get("mobile") or "—"}</div></div>
      </div>
      <div style='background:#1e293b;border-radius:6px;padding:8px 10px;
                  font-family:monospace;font-size:0.75rem;color:#94a3b8;
                  word-break:break-all'>{url}</div>
    </div>
    """, unsafe_allow_html=True)

    # ── CUSTOMER CLAIMED — show reference + confirm/reject ────────────────
    if status == "CUSTOMER_CLAIMED":
        ref    = str(active.get("customer_ref") or "")
        method = str(active.get("customer_method") or "")
        note   = str(active.get("customer_note") or "")
        st.warning(f"🔔 **Customer reports payment via {method}** — Ref: `{ref}`")
        if note:
            st.caption(f"Customer note: {note}")

        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            if st.button("✅ Confirm & Record", type="primary",
                         key=f"plm_confirm_{token}", use_container_width=True):
                try:
                    from modules.security.roles import current_user_name
                    _by = current_user_name()
                    if not isinstance(_by, str):
                        _by = getattr(_by, "name", "staff")
                except: _by = "staff"
                try:
                    confirm_payment(token, _by)
                    st.success("✅ Payment confirmed and recorded!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        with c2:
            if st.button("❌ Reject Claim", key=f"plm_reject_{token}",
                         use_container_width=True):
                void_link(token, "Claim rejected by staff")
                st.warning("Claim rejected. Generate a new link if needed.")
                st.rerun()
        with c3:
            st.link_button("💬 WhatsApp", wa_url, use_container_width=True)
        return

    # ── Action buttons ────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
    with c1:
        st.link_button("💬 Send WhatsApp", wa_url, use_container_width=True)
    with c2:
        # Copy to clipboard via JS trick
        if st.button("📋 Copy Link", key=f"plm_copy_{token}", use_container_width=True):
            st.code(url)
            st.caption("☝️ Select all and copy")
    with c3:
        if st.button("🔄 New Link", key=f"plm_new_{token}", use_container_width=True):
            void_link(token, "Replaced by staff")
            st.rerun()
    with c4:
        if st.button("🗑️ Void Link", key=f"plm_void_{token}", use_container_width=True):
            void_link(token, "Voided by staff")
            st.info("Link voided.")
            st.rerun()

    if status == "PENDING":
        if st.button("📤 Mark as Sent (manual)", key=f"plm_sent_{token}",
                     use_container_width=True):
            mark_link_sent(token)
            st.rerun()


def _render_new_link_form(order_id, order_no, party_name, mobile,
                           all_lines, order_type):
    """Form to generate a new payment link."""
    # Compute amount due
    from modules.billing.payment_manager import _compute_order_total, _q as _pmq
    # order_id is already a param — no need to read from `order` dict
    total = _compute_order_total(all_lines, order_type, order_id)

    # Sum ALL payments against this order: advances + challan direct + invoice direct
    _all_paid = _pmq("""
        SELECT COALESCE(SUM(p.amount), 0) AS tot
        FROM payments p
        WHERE COALESCE(p.is_deleted, FALSE) = FALSE
          AND (
              -- Advance at punch time
              p.advance_for_order_id::text = %(oid)s
              OR
              -- Direct challan payments
              p.challan_id IN (
                  SELECT c.id FROM challans c
                  WHERE c.order_ids IS NOT NULL AND %(oid)s = ANY(c.order_ids::text[])
                    AND c.status NOT IN ('CANCELLED','VOID')
              )
              OR
              -- Invoice payments
              p.invoice_id IN (
                  SELECT i.id FROM invoices i
                  WHERE i.order_ids IS NOT NULL AND %(oid)s = ANY(i.order_ids::text[])
                    AND i.status NOT IN ('CANCELLED','VOID')
              )
          )
    """, {"oid": order_id})
    already_paid = float((_all_paid[0]["tot"] if _all_paid else 0) or 0)
    balance = round(max(total - already_paid, 0), 2)

    if balance <= 0.01:
        st.success("✅ Fully paid — no payment link needed.")
        return

    st.markdown(f"""
    <div style='background:#1e293b;border-radius:8px;padding:12px 16px;margin-bottom:12px'>
      <div style='color:#64748b;font-size:0.72rem;font-weight:600;margin-bottom:4px'>
        GENERATE PAYMENT LINK
      </div>
      <div style='color:#94a3b8;font-size:0.82rem'>
        Order Total: {_fc(total)} · Already Paid: {_fc(already_paid)} ·
        <b style='color:#f59e0b'>Balance: {_fc(balance)}</b>
      </div>
    </div>
    """, unsafe_allow_html=True)

    with st.form(key=f"plm_form_{order_id[:8]}"):
        cf1, cf2 = st.columns([1, 1])
        with cf1:
            lnk_mobile = st.text_input("📱 Customer Mobile",
                                        value=mobile, placeholder="10-digit mobile")
        with cf2:
            lnk_amount = st.number_input("💰 Amount to Collect (₹)",
                                          min_value=0.01, value=float(balance), step=0.01)
        lnk_desc = st.text_input("Description (optional)",
                                   value=f"Order {order_no} — Balance Payment",
                                   placeholder="Shown to customer")
        lnk_expiry = st.slider("Link validity (hours)", 1, 168, 72)

        submitted = st.form_submit_button("🔗 Generate Payment Link",
                                           type="primary", use_container_width=True)
        if submitted:
            if not lnk_mobile or len(lnk_mobile.strip()) < 10:
                st.error("❌ Enter a valid 10-digit mobile number.")
            else:
                try:
                    from modules.security.roles import current_user_name
                    _by = current_user_name()
                    if not isinstance(_by, str):
                        _by = getattr(_by, "name", "staff")
                except: _by = "staff"
                result = create_payment_link(
                    order_id     = order_id,
                    order_no     = order_no,
                    party_name   = party_name,
                    mobile       = lnk_mobile.strip(),
                    amount       = float(lnk_amount),
                    description  = lnk_desc,
                    expiry_hours = lnk_expiry,
                    created_by   = _by,
                )
                st.success(f"✅ Payment link generated! Token: `{result['token']}`")
                st.code(result["url"])
                st.link_button(
                    "💬 Open WhatsApp Now", result["whatsapp_url"],
                    use_container_width=True
                )
                mark_link_sent(result["token"])
                st.rerun()


def _render_hold_controls(order: dict, order_id: str,
                           order_no: str, status: str):
    """Put order on hold / release hold."""
    st.markdown("---")
    is_on_hold = (status == "PAYMENT_PENDING")

    if is_on_hold:
        st.error("🔴 **Order is ON HOLD** — waiting for payment before processing.")
        if st.button("🟢 Release Hold (payment received)", key=f"plm_release_{order_id[:8]}",
                     use_container_width=True):
            release_order_hold(order_id, order_no)
            # Update in-memory order
            order["status"] = "CONFIRMED"
            st.success("✅ Hold released — order set to CONFIRMED.")
            st.rerun()
    else:
        with st.expander("🔴 Put Order on Hold (awaiting payment)"):
            hold_reason = st.text_input("Hold reason (optional)", key=f"plm_hold_reason_{order_id[:8]}")
            if st.button("🔴 Put On Hold", key=f"plm_hold_{order_id[:8]}",
                         use_container_width=True):
                put_order_on_hold(order_id, order_no, hold_reason)
                order["status"] = "PAYMENT_PENDING"
                st.warning("🔴 Order put on hold. It will not be dispatched until payment is confirmed.")
                st.rerun()


def _render_settings_form():
    """Quick settings editor for shop/UPI details."""
    shop_name = st.text_input("Shop Name",    value=get_setting("shop_name", "DV Optics"), key="plm_sn")
    upi_id    = st.text_input("UPI ID",       value=get_setting("shop_upi_id", ""),         key="plm_upi")
    upi_name  = st.text_input("UPI Name",     value=get_setting("shop_upi_name", "DV Optics"), key="plm_upn")
    base_url  = st.text_input("App Base URL", value=get_base_url(),                          key="plm_url",
                               help="The public URL of your ERP — used in payment links")
    if st.button("💾 Save Settings", key="plm_save_settings"):
        set_setting("shop_name",     shop_name)
        set_setting("shop_upi_id",   upi_id)
        set_setting("shop_upi_name", upi_name)
        set_setting("app_base_url",  base_url)
        st.success("✅ Settings saved!")


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER-FACING PAYMENT PAGE  (no auth — accessed via ?pay=TOKEN)
# ═══════════════════════════════════════════════════════════════════════════

def render_payment_page(token: str):
    """
    Customer-facing page. No Streamlit auth required.
    Called from app.py before the login gate.
    """
    st.set_page_config(
        page_title="Pay Now — DV Optics",
        page_icon="💳",
        layout="centered",
    )

    lnk = get_link_by_token(token)
    shop_name = get_shop_name()

    # ── Header ────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div style='text-align:center;padding:20px 0 10px'>
      <div style='font-size:2rem'>👓</div>
      <div style='font-size:1.4rem;font-weight:700;color:#f1f5f9'>{shop_name}</div>
      <div style='color:#64748b;font-size:0.82rem'>Secure Payment</div>
    </div>
    """, unsafe_allow_html=True)

    if not lnk:
        st.error("❌ Invalid or expired payment link.")
        st.info("Please contact the shop for a new link.")
        return

    status = str(lnk.get("status") or "").upper()
    amount = float(lnk.get("amount") or 0)
    name   = str(lnk.get("party_name") or "Customer")
    order_no = str(lnk.get("order_no") or "")
    expires  = lnk.get("expires_at")
    upi_id   = get_upi_id()
    upi_name = get_upi_name()

    # ── Handle terminal states ────────────────────────────────────────────
    if status == "EXPIRED":
        st.error("⏰ This payment link has expired.")
        st.info("Please contact the shop for a new link.")
        return

    if status == "VOIDED":
        st.error("❌ This payment link has been cancelled.")
        st.info("Please contact the shop.")
        return

    if status in ("PAID",):
        st.success("✅ Payment already received. Thank you!")
        st.balloons()
        return

    if status == "CUSTOMER_CLAIMED":
        st.info("⏳ Your payment is being verified by our team. We'll confirm shortly.")
        st.caption(f"Reference: {lnk.get('customer_ref') or '—'}")
        return

    # ── Expiry check ──────────────────────────────────────────────────────
    if expires:
        try:
            exp_dt = expires if hasattr(expires, "timestamp") else \
                     datetime.datetime.fromisoformat(str(expires))
            if exp_dt < datetime.datetime.now():
                _q("UPDATE payment_links SET status='EXPIRED' WHERE token=%(t)s", {"t": token})
                st.error("⏰ This payment link has expired.")
                return
        except: pass

    # ── Order card ────────────────────────────────────────────────────────
    st.markdown(f"""
    <div style='background:#1e293b;border-radius:14px;padding:20px 24px;
                margin:10px 0;text-align:center'>
      <div style='color:#64748b;font-size:0.75rem;font-weight:600;
                  letter-spacing:.08em;text-transform:uppercase'>Payment Due</div>
      <div style='font-size:2.8rem;font-weight:800;color:#f59e0b;margin:6px 0'>
        ₹{amount:,.2f}
      </div>
      <div style='color:#94a3b8;font-size:0.9rem'>
        Hi <b style='color:#f1f5f9'>{name}</b>, your order
        <b style='color:#f1f5f9'>#{order_no}</b> is ready.
      </div>
      {f"<div style='color:#64748b;font-size:0.72rem;margin-top:8px'>Valid till {_fd(expires)}</div>" if expires else ""}
    </div>
    """, unsafe_allow_html=True)

    # ── UPI payment instructions ──────────────────────────────────────────
    if upi_id:
        # UPI deep link
        upi_link = (
            f"upi://pay?pa={urllib.parse.quote(upi_id)}"
            f"&pn={urllib.parse.quote(upi_name)}"
            f"&am={amount:.2f}"
            f"&tn={urllib.parse.quote('Order ' + order_no)}"
            f"&cu=INR"
        )
        # QR via upiqr.in (no library needed)
        qr_url = (
            f"https://upiqr.in/api/qr?vpa={urllib.parse.quote(upi_id)}"
            f"&name={urllib.parse.quote(upi_name)}"
            f"&amount={amount:.2f}"
            f"&trxnote={urllib.parse.quote('Order ' + order_no)}"
        )

        st.markdown(f"""
        <div style='background:#0f172a;border:1px solid #334155;border-radius:12px;
                    padding:18px 24px;margin:12px 0;text-align:center'>
          <div style='color:#64748b;font-size:0.72rem;font-weight:600;
                      text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px'>
            Pay via UPI
          </div>
          <img src="{qr_url}" width="180"
               style='border-radius:10px;background:white;padding:8px;margin-bottom:12px'
               onerror="this.style.display='none'">
          <div style='background:#1e293b;border-radius:8px;padding:10px 14px;
                      font-family:monospace;font-size:1rem;color:#f1f5f9;margin:8px 0'>
            {upi_id}
          </div>
          <div style='color:#64748b;font-size:0.75rem'>
            Scan QR or copy UPI ID · Amount: <b style='color:#f59e0b'>₹{amount:,.2f}</b>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.link_button("📲 Pay with UPI App", upi_link, use_container_width=True)
    else:
        st.info(f"Please pay ₹{amount:,.2f} to **{shop_name}** and note the reference number.")

    # ── Customer claims payment ───────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### ✅ After Payment")
    st.caption("Enter your payment reference and tap 'I have paid' — our team will verify and confirm.")

    with st.form("payment_claim_form"):
        method = st.selectbox("Payment Method",
                               ["UPI", "CASH", "NEFT", "RTGS", "CHEQUE", "CARD"],
                               key="pg_method")
        ref    = st.text_input("Reference / Transaction ID",
                               placeholder="UPI Ref No. or transaction ID",
                               key="pg_ref")
        note   = st.text_area("Any note for the shop (optional)",
                               placeholder="e.g. 'Paid via PhonePe at 3pm'",
                               key="pg_note", max_chars=300)
        claimed = st.form_submit_button("✅ I Have Paid", use_container_width=True)

        if claimed:
            if not ref.strip():
                st.error("❌ Please enter the transaction reference number.")
            else:
                ok = customer_claim_payment(token, ref.strip(), method, note)
                if ok:
                    st.success("✅ Your payment has been submitted for verification. Thank you!")
                    st.info(f"Reference noted: **{ref.strip()}**")
                    st.balloons()
                else:
                    st.error("❌ Could not record your payment. Please contact the shop.")

    st.markdown(f"""
    <div style='text-align:center;color:#475569;font-size:0.72rem;margin-top:20px;padding:12px'>
      Need help? Contact {shop_name}<br>
      <span style='font-family:monospace'>{token}</span>
    </div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# BACKOFFICE PENDING CLAIMS BADGE (for dashboard)
# ═══════════════════════════════════════════════════════════════════════════

def get_pending_claims_count() -> int:
    """Returns count of CUSTOMER_CLAIMED links awaiting staff verification."""
    rows = _q("""
        SELECT COUNT(*) AS cnt FROM payment_links
        WHERE status = 'CUSTOMER_CLAIMED'
    """)
    return int((rows[0]["cnt"] if rows else 0) or 0)


def render_pending_claims_dashboard():
    """
    Mini-dashboard shown in Backoffice sidebar or main page
    when any customer has claimed payment but staff hasn't confirmed.
    """
    claims = _q("""
        SELECT pl.token, pl.order_no, pl.party_name, pl.amount,
               pl.customer_ref, pl.customer_method, pl.customer_note,
               pl.updated_at
        FROM payment_links pl
        WHERE pl.status = 'CUSTOMER_CLAIMED'
        ORDER BY pl.updated_at ASC
    """)
    if not claims:
        return

    st.warning(f"🔔 **{len(claims)} customer payment(s) awaiting verification!**")
    for c in claims:
        with st.container(border=True):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.markdown(
                    f"**{c.get('party_name')}** · Order `{c.get('order_no')}`  \n"
                    f"Ref: `{c.get('customer_ref')}` via **{c.get('customer_method')}**"
                )
                if c.get("customer_note"):
                    st.caption(c["customer_note"])
            with col2:
                st.metric("Amount", _fc(c.get("amount")))
            with col3:
                if st.button("✅ Confirm", key=f"plm_dash_confirm_{c['token']}",
                             use_container_width=True):
                    try:
                        from modules.security.roles import current_user_name
                        _by = current_user_name()
                        if not isinstance(_by, str):
                            _by = getattr(_by, "name", "staff")
                    except: _by = "staff"
                    try:
                        confirm_payment(c["token"], _by)
                        st.success("✅ Confirmed!")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
