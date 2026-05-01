"""
Order Charges Panel
====================
Manages fitting, colouring, courier and misc charges per order.
Embedded in Billing Summary tab — charges added here flow into
challan/invoice grand totals.
"""

import streamlit as st
from typing import Dict, List, Optional
import json

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {}); return True
    except Exception as e:
        st.error(f"DB write: {e}"); return False

def _fc(v):
    try: return f"₹{float(v):,.2f}"
    except: return "₹0.00"

# ── Charge type config — imported from central business_rules ─────────────
# To add a new service: edit modules/core/business_rules.SERVICE_CHARGE_TYPES only.
from modules.core.business_rules import SERVICE_CHARGE_TYPES as _CHARGE_TYPES, get_charge_type

# ── Fetch / Save ──────────────────────────────────────────────────────────

def _ensure_order_charges_table() -> None:
    """Create order_charges and related tables if they don't exist yet."""
    _w("""
        CREATE TABLE IF NOT EXISTS order_charges (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id        UUID NOT NULL,
            charge_type     TEXT NOT NULL,
            description     TEXT,
            amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
            gst_percent     NUMERIC(5,2)  NOT NULL DEFAULT 0,
            gst_amount      NUMERIC(12,2) GENERATED ALWAYS AS
                                (ROUND(amount * gst_percent / 100, 2)) STORED,
            total_amount    NUMERIC(12,2) GENERATED ALWAYS AS
                                (ROUND(amount + amount * gst_percent / 100, 2)) STORED,
            courier_company TEXT,
            tracking_no     TEXT,
            is_confirmed    BOOLEAN DEFAULT TRUE,
            is_locked       BOOLEAN DEFAULT FALSE,
            created_by      TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _w("""
        CREATE TABLE IF NOT EXISTS challan_service_charges (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            challan_id      UUID NOT NULL,
            order_id        UUID,
            charge_type     TEXT,
            description     TEXT,
            base_amount     NUMERIC(12,2),
            gst_percent     NUMERIC(5,2),
            gst_amount      NUMERIC(12,2),
            total_amount    NUMERIC(12,2),
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    _w("""
        CREATE TABLE IF NOT EXISTS courier_companies (
            id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name      TEXT UNIQUE NOT NULL,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)


def fetch_charges(order_id: str) -> List[Dict]:
    _ensure_order_charges_table()
    return _q("""
        SELECT id::text, charge_type, description, amount,
               gst_percent, gst_amount, total_amount,
               courier_company, tracking_no, is_confirmed,
               created_at
        FROM order_charges
        WHERE order_id::text = %(oid)s
        ORDER BY created_at
    """, {"oid": order_id})

def fetch_courier_companies() -> List[str]:
    rows = _q("SELECT name FROM courier_companies WHERE is_active=TRUE ORDER BY name")
    return [r["name"] for r in rows]

def save_charge(order_id: str, charge_type: str, description: str,
                amount: float, gst_pct: float,
                courier_company: str = "", tracking_no: str = "",
                created_by: str = "System") -> bool:
    # Always compute gst_amount and total_amount — never leave as NULL
    amt       = round(float(amount or 0), 2)
    gst_pct   = round(float(gst_pct or 0), 2)
    gst_amt   = round(amt * gst_pct / 100, 2)
    total_amt = round(amt + gst_amt, 2)
    # gst_amount and total_amount are GENERATED ALWAYS AS columns —
    # they auto-compute from amount and gst_percent; do NOT include them in INSERT
    # Fix 5: UPSERT — if same charge_type already exists for this order, UPDATE it
    # Prevents duplicate FITTING/COLOURING/COURIER charges
    return _w("""
        INSERT INTO order_charges
            (id, order_id, charge_type, description, amount, gst_percent,
             courier_company, tracking_no, is_confirmed, created_by)
        VALUES
            (gen_random_uuid(), %(oid)s::uuid, %(ct)s, %(desc)s,
             %(amt)s, %(gst)s,
             %(cc)s, %(tn)s, TRUE, %(by)s)
        ON CONFLICT (order_id, charge_type) DO UPDATE
            SET description     = EXCLUDED.description,
                amount          = EXCLUDED.amount,
                gst_percent     = EXCLUDED.gst_percent,
                courier_company = EXCLUDED.courier_company,
                tracking_no     = EXCLUDED.tracking_no,
                is_confirmed    = TRUE,
                updated_at      = NOW()
    """, {
        "oid": order_id, "ct": charge_type, "desc": description,
        "amt": amt, "gst": gst_pct,
        "cc": courier_company, "tn": tracking_no, "by": created_by
    })

def update_charge(charge_id: str, description: str, amount: float,
                  gst_pct: float, courier_company: str = "",
                  tracking_no: str = "") -> bool:
    amt       = round(float(amount or 0), 2)
    gst_pct   = round(float(gst_pct or 0), 2)
    gst_amt   = round(amt * gst_pct / 100, 2)
    total_amt = round(amt + gst_amt, 2)
    # gst_amount and total_amount are GENERATED ALWAYS AS columns —
    # they recompute automatically when amount / gst_percent change
    return _w("""
        UPDATE order_charges
        SET description=%(d)s, amount=%(a)s, gst_percent=%(g)s,
            courier_company=%(cc)s, tracking_no=%(tn)s,
            is_confirmed=TRUE, updated_at=NOW()
        WHERE id=%(id)s::uuid
    """, {"d": description, "a": amt, "g": gst_pct,
          "cc": courier_company, "tn": tracking_no, "id": charge_id})

def delete_charge(charge_id: str) -> bool:
    """
    Delete a service charge.
    BLOCKED if the charge is locked — meaning its order already has a
    confirmed challan. Use a Credit Note to adjust post-challan amounts.
    """
    locked = _q("""
        SELECT is_locked FROM order_charges
        WHERE id = %(cid)s::uuid
    """, {"cid": charge_id})
    if locked and locked[0].get("is_locked"):
        st.error(
            "❌ Cannot delete — this charge is locked to a confirmed challan. "
            "Raise a Credit Note to adjust the amount."
        )
        return False
    return _w("DELETE FROM order_charges WHERE id=%(id)s::uuid",
              {"id": charge_id})

# ── Auto-detect charges from lens_params ─────────────────────────────────

def _detect_pending_charges(order: Dict, all_lines: List[Dict],
                             existing_charges: List[Dict]) -> List[Dict]:
    """
    Read lens_params from order lines and suggest charges not yet added.
    Returns list of {charge_type, description, hint}.
    """
    suggestions = []
    existing_types = {c["charge_type"] for c in existing_charges}

    for line in all_lines:
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = json.loads(lp)
            except: lp = {}

        # Fitting
        if lp.get("fitting_required") and "FITTING" not in existing_types:
            ft = lp.get("fitting_type") or "Full Rim"
            suggestions.append({
                "charge_type": "FITTING",
                "description": f"Frame Fitting — {ft}",
                "hint": "Price TBD at order stage",
            })
            existing_types.add("FITTING")

        # Colouring
        colour = lp.get("colour") or ""
        if colour and colour != "None" and "COLOURING" not in existing_types:
            suggestions.append({
                "charge_type": "COLOURING",
                "description": f"Lens Colouring — {colour}",
                "hint": "Price TBD at order stage",
            })
            existing_types.add("COLOURING")

    return suggestions



# ══════════════════════════════════════
# CONSULTATION AUTO-PREFILL
# ══════════════════════════════════════

def _ensure_consultation_charge(order_id, order):
    # Auto-insert CONSULTATION into order_charges exactly once so it
    # appears pre-loaded at the TOP of the backoffice charges panel
    # (above Fitting / Colouring / Courier).  Idempotent: no-op if a
    # CONSULTATION row already exists for this order.
    existing = _q(
        "SELECT id FROM order_charges "
        "WHERE order_id::text = %(oid)s AND charge_type = 'CONSULTATION'",
        {"oid": order_id}
    )
    if existing:
        return  # already saved

    # Source 1: service_lines populated by backoffice_helpers onto order dict
    for svc in (order.get("service_lines") or []):
        desc = str(
            svc.get("product_name") or
            svc.get("description") or
            "Consultation Fee"
        )
        if "consult" not in desc.lower():
            continue
        amt = float(
            svc.get("billing_total") or
            svc.get("unit_price") or
            svc.get("amount") or 0
        )
        if amt > 0:
            save_charge(
                order_id, "CONSULTATION", desc, amt, gst_pct=0,
                created_by="System (auto-prefill from retail)"
            )
            return

    # Source 2: direct consultation_fee field on order dict
    direct_fee = float(order.get("consultation_fee") or 0)
    if direct_fee > 0:
        save_charge(
            order_id, "CONSULTATION",
            "Consultation Fee (from retail)",
            direct_fee, gst_pct=0,
            created_by="System (auto-prefill)"
        )

# ══════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════

def render_order_charges_panel(order: Dict, all_lines: List[Dict]) -> float:
    """
    Render the full charges panel.
    Returns total charges amount (excl GST) to add to billing grand total.
    """
    order_id = str(order.get("id") or "")
    if not order_id:
        return 0.0

    # Auto-insert consultation fee before first fetch (idempotent)
    _ensure_consultation_charge(order_id, order)
    charges  = fetch_charges(order_id)   # re-fetch after possible insert
    couriers = fetch_courier_companies()
    user     = st.session_state.get("user_name", "System")

    # ── Auto-suggest from lens_params ─────────────────────────────────────
    suggestions = _detect_pending_charges(order, all_lines, charges)
    if suggestions:
        st.markdown(
            "<div style='background:#1a1000;border:1px solid #f59e0b44;"
            "border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
            "<div style='color:#f59e0b;font-weight:700;font-size:0.78rem'>"
            "⚠️ Unpriced services detected from order</div>",
            unsafe_allow_html=True
        )
        for s in suggestions:
            cfg = get_charge_type(s["charge_type"])
            st.markdown(
                f"<div style='color:#fcd34d;font-size:0.75rem;padding:2px 0'>"
                f"{cfg.get('icon','')} {s['description']} — "
                f"<span style='color:#92400e'>{s['hint']}</span></div>",
                unsafe_allow_html=True
            )
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Existing charges ──────────────────────────────────────────────────
    charges_subtotal = 0.0
    charges_gst      = 0.0

    if charges:
        st.markdown("**Added Charges**")
        for ch in charges:
            cid   = ch["id"]
            ctype = ch["charge_type"]
            cfg   = get_charge_type(ctype)
            amt   = float(ch.get("amount") or 0)
            gst_a = float(ch.get("gst_amount") or 0)
            tot   = float(ch.get("total_amount") or 0)
            charges_subtotal += amt
            charges_gst      += gst_a

            with st.container():
                rc1, rc2, rc3, rc4, rc5 = st.columns([0.4, 2.5, 1.2, 1.2, 0.6])
                with rc1:
                    st.markdown(
                        f"<div style='font-size:1.3rem;text-align:center'>"
                        f"{cfg['icon']}</div>",
                        unsafe_allow_html=True)
                with rc2:
                    # Consultation charges get a locked badge — they came from retail
                    _is_consult = (ctype == "CONSULTATION")
                    _badge = (
                        "<span style='background:#14532d;color:#4ade80;font-size:0.6rem;"
                        "border-radius:4px;padding:1px 5px;margin-left:6px;"
                        "font-weight:700;letter-spacing:.04em'>"
                        "🔒 PRE-LOADED</span>"
                    ) if _is_consult else ""
                    st.markdown(
                        f"<div style='color:#e2e8f0;font-size:0.8rem;font-weight:600'>"
                        f"{ch.get('description') or cfg['label']}{_badge}</div>"
                        f"<div style='color:#64748b;font-size:0.68rem'>"
                        f"{cfg['label']}"
                        + (f" · {ch['courier_company']}" if ch.get('courier_company') else "")
                        + (f" · {ch['tracking_no']}" if ch.get('tracking_no') else "")
                        + "</div>",
                        unsafe_allow_html=True)
                with rc3:
                    st.markdown(
                        f"<div style='color:#94a3b8;font-size:0.7rem'>Base</div>"
                        f"<div style='color:#e2e8f0;font-weight:700'>{_fc(amt)}</div>",
                        unsafe_allow_html=True)
                with rc4:
                    st.markdown(
                        f"<div style='color:#94a3b8;font-size:0.7rem'>"
                        f"GST {ch.get('gst_percent',18):.0f}%</div>"
                        f"<div style='color:{cfg['color']};font-weight:700'>{_fc(tot)}</div>",
                        unsafe_allow_html=True)
                with rc5:
                    if st.button("🗑", key=f"del_charge_{cid}",
                                 help="Remove charge"):
                        if delete_charge(cid):
                            st.rerun()

                # Inline edit
                with st.expander("✏️ Edit", expanded=False):
                    _ea, _egst = st.columns(2)
                    with _ea:
                        _new_amt = st.number_input(
                            "Amount ₹", value=amt, min_value=0.0,
                            step=10.0, key=f"ec_amt_{cid}")
                    with _egst:
                        _new_gst = st.number_input(
                            "GST %", value=float(ch.get("gst_percent",18)),
                            min_value=0.0, max_value=28.0,
                            step=0.5, key=f"ec_gst_{cid}")
                    _new_desc = st.text_input(
                        "Description",
                        value=ch.get("description") or "",
                        key=f"ec_desc_{cid}")
                    _new_cc = _new_tn = ""
                    if ctype == "COURIER":
                        _ec1, _ec2 = st.columns(2)
                        with _ec1:
                            _new_cc = st.selectbox(
                                "Courier Co.", options=couriers,
                                index=couriers.index(ch["courier_company"])
                                if ch.get("courier_company") in couriers else 0,
                                key=f"ec_cc_{cid}")
                        with _ec2:
                            _new_tn = st.text_input(
                                "Tracking No.",
                                value=ch.get("tracking_no") or "",
                                key=f"ec_tn_{cid}")
                    if st.button("💾 Save", key=f"ec_save_{cid}",
                                 use_container_width=True):
                        if update_charge(cid, _new_desc, _new_amt,
                                         _new_gst, _new_cc, _new_tn):
                            st.success("Saved"); st.rerun()

                st.markdown(
                    "<hr style='margin:4px 0;border-color:#1e293b'>",
                    unsafe_allow_html=True)

    # ── Add new charge ────────────────────────────────────────────────────
    _add_key = f"show_add_charge_{order_id}"
    if _add_key not in st.session_state:
        st.session_state[_add_key] = False

    if not st.session_state[_add_key]:
        # ── Dynamic service buttons — driven by business_rules.SERVICE_CHARGE_TYPES ──
        # Build list: skip CONSULTATION if already saved (idempotent guard)
        _existing_ctypes = {c["charge_type"] for c in charges}
        _types = [
            ct for ct in _CHARGE_TYPES.keys()
            if not (ct == "CONSULTATION" and "CONSULTATION" in _existing_ctypes)
        ]
        if not _types:
            st.caption("🟢 All charge types already added.")
        else:
            _cols  = st.columns(min(len(_types), 4))   # max 4 per row
            for _ci, _ct in enumerate(_types):
                _cfg = _CHARGE_TYPES[_ct]
                with _cols[_ci % 4]:
                    if st.button(
                        f"{_cfg['icon']} + {_cfg['label']}",
                        key=f"add_{_ct.lower()}_{order_id}",
                        use_container_width=True,
                    ):
                        st.session_state[_add_key] = _ct
                # Start new row after every 4
                if (_ci + 1) % 4 == 0 and _ci < len(_types) - 1:
                    _cols = st.columns(min(len(_types) - _ci - 1, 4))
    else:
        ctype = st.session_state[_add_key]
        cfg   = get_charge_type(ctype)

        st.markdown(
            f"<div style='background:#0f172a;border:1px solid {cfg['color']}44;"
            f"border-radius:8px;padding:12px;margin:6px 0'>"
            f"<div style='color:{cfg['color']};font-weight:700'>"
            f"{cfg['icon']} Add {cfg['label']} Charge</div>",
            unsafe_allow_html=True)

        # Pre-fill description from lens_params suggestion
        _prefill_desc = ""
        for s in suggestions:
            if s["charge_type"] == ctype:
                _prefill_desc = s["description"]
                break

        _na1, _na2 = st.columns(2)
        with _na1:
            new_desc = st.text_input(
                "Description",
                value=_prefill_desc,
                placeholder=f"{cfg['label']} service description",
                key=f"na_desc_{order_id}_{ctype}")
        with _na2:
            new_amt  = st.number_input(
                "Amount ₹", min_value=0.0, step=10.0,
                key=f"na_amt_{order_id}_{ctype}")

        new_gst  = st.number_input(
            "GST %", min_value=0.0, max_value=28.0,
            value=float(cfg["default_gst"]), step=0.5,
            key=f"na_gst_{order_id}_{ctype}")

        # Courier-specific fields
        new_cc = new_tn = ""
        if ctype == "COURIER":
            _nc1, _nc2 = st.columns(2)
            with _nc1:
                new_cc = st.selectbox(
                    "Courier Company", options=couriers,
                    key=f"na_cc_{order_id}")
            with _nc2:
                new_tn = st.text_input(
                    "Tracking Number",
                    placeholder="AWB / tracking no.",
                    key=f"na_tn_{order_id}")

        # Live total preview
        if new_amt > 0:
            _prev_gst = round(new_amt * new_gst / 100, 2)
            _prev_tot = new_amt + _prev_gst
            st.markdown(
                f"<div style='color:#64748b;font-size:0.75rem;margin:4px 0'>"
                f"Base {_fc(new_amt)} + GST {_fc(_prev_gst)} = "
                f"<b style='color:#10b981'>{_fc(_prev_tot)}</b></div>",
                unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        _sb1, _sb2 = st.columns(2)
        with _sb1:
            if st.button("✅ Add Charge", type="primary",
                         use_container_width=True,
                         key=f"na_save_{order_id}_{ctype}"):
                if new_amt <= 0:
                    st.error("Enter amount > 0")
                else:
                    if save_charge(order_id, ctype,
                                   new_desc or cfg["label"],
                                   new_amt, new_gst,
                                   new_cc, new_tn, user):
                        st.session_state[_add_key] = False
                        st.rerun()
        with _sb2:
            if st.button("✕ Cancel", use_container_width=True,
                         key=f"na_cancel_{order_id}_{ctype}"):
                st.session_state[_add_key] = False
                st.rerun()

    # ── Charges total summary ─────────────────────────────────────────────
    if charges:
        charges_total = charges_subtotal + charges_gst
        st.markdown(
            f"<div style='background:#0d1f0d;border:1px solid #10b98144;"
            f"border-radius:8px;padding:10px 14px;margin-top:8px;"
            f"display:flex;justify-content:space-between;align-items:center'>"
            f"<span style='color:#94a3b8;font-size:0.78rem'>Service Charges Total</span>"
            f"<span style='color:#10b981;font-weight:800;font-size:1rem'>"
            f"{_fc(charges_subtotal)} + GST {_fc(charges_gst)} = "
            f"<b>{_fc(charges_total)}</b></span></div>",
            unsafe_allow_html=True)

    return charges_subtotal
