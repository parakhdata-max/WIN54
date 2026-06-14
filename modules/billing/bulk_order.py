"""
modules/billing/bulk_order.py
==============================
Bulk Order Screen — fast counter billing for ready stock.

Flow:
  1. SELECT PARTY      → auto-detects RETAIL / WHOLESALE
  2. BUILD CART        → scan/search products, eye side optional,
                         qty checked against live stock (no oversell),
                         progressive B eye splits R+L stock automatically
  3. PLACE ORDER       → creates COUNTER_SALE order + order_lines
  4. CREATE CHALLAN    → challan from order
  5. CREATE INVOICE    → RETAIL: only after payment | WHOLESALE: credit allowed
  6. PAYMENT           → inline collection with print/WhatsApp

Progressive B logic:
  If eye_side = B and inventory_stock has separate R + L rows for that power
  → bills as ONE "Pair" line (qty=1 means 1 pair)
  → internally deducts 1 from R stock + 1 from L stock
  Contact lenses have a single B row → used directly, no split.
"""

import uuid
import datetime
import streamlit as st
try:
    from modules.ophthalmic_billing import (
        render_ophthalmic_selector as _oph_sel,
        ophthalmic_unit_price      as _oph_price,
        ophthalmic_display_name    as _oph_name,
        render_availability_grid   as _oph_grid,
    )
    _HAS_OPH = True
except ImportError:
    _HAS_OPH = False
    _oph_sel = None; _oph_price = None; _oph_name = None; _oph_grid = None
try:
    from modules.price_governor import get_billing_price, validate_price
    from modules.price_dropdown_ui import render_price_selector as _render_price
except Exception:
    _render_price = None
_price_selector = None
try:
    from modules.power_intelligence_ui import render_power_intelligence_panel as _pi_panel
except Exception:
    _pi_panel = None
import streamlit.components.v1 as _stc
from typing import List, Dict, Optional
try:
    from modules.core.eye_side_normalizer import normalize_eye_side
except ImportError:
    def normalize_eye_side(v, **_):  # noqa: F811
        if not v:
            return "B"
        _k = str(v).strip().upper()
        if _k in ("R", "RIGHT", "RE"):   return "R"
        if _k in ("L", "LEFT", "LE"):    return "L"
        if _k in ("S", "SVC", "SERVICE", "SERVICES"): return "SERVICE"
        return "B"


# ── Keyboard helpers ──────────────────────────────────────────────────────────

def _autofocus_scan():
    """Auto-focus the barcode/scan input on page load."""
    _stc.html("""<script>
    setTimeout(function() {
        var inputs = window.parent.document.querySelectorAll('input[type="text"]');
        for (var i = 0; i < inputs.length; i++) {
            var p = inputs[i].placeholder || '';
            if (p.indexOf('Scan') >= 0 || p.indexOf('barcode') >= 0) {
                inputs[i].focus(); return;
            }
        }
        if (inputs.length) inputs[0].focus();
    }, 200);
    </script>""", height=0)


def _enter_to_click(selector='button[kind="primaryFormSubmit"], button[data-testid="baseButton-primary"]'):
    """Wire Enter key → primary button click (skip textarea)."""
    _stc.html(f"""<script>
    (function() {{
        var done = false;
        window.parent.document.addEventListener('keydown', function(e) {{
            if (done) return;
            if (e.key !== 'Enter' || e.shiftKey) return;
            if (e.target.tagName === 'TEXTAREA') return;
            var btn = window.parent.document.querySelector('{selector}');
            if (btn && !btn.disabled) {{
                done = true; e.preventDefault(); btn.click();
                setTimeout(function(){{ done = false; }}, 600);
            }}
        }}, true);
    }})();
    </script>""", height=0)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _rq(sql: str, params=None) -> list:
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _rw(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params or {})
    except Exception as e:
        st.error(f"DB error: {e}")
        return False


def _scalar(sql: str, params=None):
    try:
        from modules.sql_adapter import run_scalar
        return run_scalar(sql, params or {})
    except Exception:
        return None


def _ensure_service_product_id(charge_type: str = "MISC", label: str = "Service Charge") -> str:
    """Find/create the lightweight Services product used for service-only bulk billing."""
    try:
        _ct = str(charge_type or "MISC").upper().strip()
        _label = str(label or _ct.title()).strip()
        _prod_name = f"{_label} Charge" if not _label.lower().endswith("charge") else _label
        rows = _rq("""
            SELECT id::text AS id FROM products
            WHERE LOWER(product_name)=LOWER(%(n)s)
              AND COALESCE(is_active, TRUE)=TRUE
            LIMIT 1
        """, {"n": _prod_name})
        if rows:
            return str(rows[0]["id"])
        pid = str(uuid.uuid4())
        _rw("""
            INSERT INTO products
                (id, product_name, main_group, category, unit,
                 gst_percent, is_active, created_at)
            VALUES (%(id)s::uuid, %(n)s, 'Services', 'Services', 'S',
                    %(gst)s, TRUE, NOW())
            ON CONFLICT DO NOTHING
        """, {"id": pid, "n": _prod_name, "gst": 18})
        return pid
    except Exception:
        return ""


def _service_master_rows(order_type: str, party_id: str = "") -> list:
    try:
        from modules.backoffice.service_master import fetch_service_types, service_price
        rows = fetch_service_types(active_only=True)
        out = []
        for r in rows:
            rr = dict(r)
            rr["default_price"] = service_price(rr, order_type, party_id=party_id)
            out.append(rr)
        return out
    except Exception:
        return []


# ── Session state init ────────────────────────────────────────────────────────

def _init():
    defaults = {
        "bo_mode":             "RETAIL",
        "bo_party_id":         None,
        "bo_party_name":       "",
        "bo_party_type":       "RETAIL",
        "bo_billing_category": "ADVANCE_BALANCE",
        "bo_credit_limit":     0.0,
        "bo_credit_days":      30,
        "bo_cart":             [],
        "bo_order_id":         None,
        "bo_order_no":         None,
        "bo_challan_no":       None,
        "bo_invoice_id":       None,
        "bo_invoice_no":       None,
        "bo_paid":             False,
        "bo_tab":              0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def bulk_hard_reset(keep_mode: bool = False):
    """
    Complete wipe of all bulk order state.
    Clears structured bo_* keys AND all widget key prefixes.
    keep_mode=True preserves bo_mode (for mode-switch reset).
    """
    _mode = st.session_state.get("bo_mode", "RETAIL")

    # ── Structured bo_* keys ──────────────────────────────────────────────
    _BO_KEYS = [
        "bo_party_id","bo_party_name","bo_party_type",
        "bo_billing_category","bo_credit_limit","bo_credit_days",
        "bo_cart","bo_order_id","bo_order_no","bo_challan_no",
        "bo_invoice_id","bo_invoice_no","bo_paid","bo_tab",
        # Payment widgets
        "bo_pay_mode","bo_pay_amount","bo_pay_ref",
        # Post-save snapshot
        "_bo_post_save_data","_bo_source_order_no",
        # Advance at order stage
        "bo_advance_mode","bo_advance_amount","bo_advance_ref","bo_collect_advance",
    ]
    for k in _BO_KEYS:
        st.session_state.pop(k, None)

    # ── Widget key prefixes ───────────────────────────────────────────────
    _PREFIXES = (
        "bo_scan", "bo_sph", "bo_cyl", "bo_axis", "bo_add",
        "bo_price", "bo_qty", "bo_eye", "bo_brand",
        "bo_search", "bo_party", "bo_mode_",
        "bo_svc_", "bo_wa_",
        "bo_print_", "bo_whatsapp",
        "bo_record_", "bo_new_", "bo_dup_",
    )
    for k in list(st.session_state.keys()):
        if any(k.startswith(p) for p in _PREFIXES):
            st.session_state.pop(k, None)

    # ── Restore mode + reinit defaults ───────────────────────────────────
    if keep_mode:
        st.session_state["bo_mode"] = _mode
    _init()


def _reset():
    """Alias kept for backward-compatible callers."""
    bulk_hard_reset(keep_mode=True)
    st.rerun()


def duplicate_current_bulk_order():
    """
    Duplicate: restore party + mode + cart snapshot.
    Clears all order/challan/invoice/payment IDs so a NEW order is created.
    Sets _bo_duplicate_mode=True.
    """
    _snap = st.session_state.get("_bo_post_save_data") or {}

    # Save source order ref before reset
    _src_ono = _snap.get("order_no","")

    # Capture what to restore
    _party_id   = _snap.get("_party_id") or st.session_state.get("bo_party_id","")
    _party_name = _snap.get("_party_name") or st.session_state.get("bo_party_name","")
    _party_type = _snap.get("_party_type") or st.session_state.get("bo_party_type","RETAIL")
    _mode       = _snap.get("_mode") or st.session_state.get("bo_mode","RETAIL")
    _bill_cat   = _snap.get("_billing_category") or st.session_state.get("bo_billing_category","ADVANCE_BALANCE")
    _cart_snap  = _snap.get("_cart") or []

    # Hard reset keeping mode
    bulk_hard_reset(keep_mode=False)

    # Restore party + mode + cart
    st.session_state["bo_mode"]             = _mode
    st.session_state["bo_party_id"]         = _party_id
    st.session_state["bo_party_name"]       = _party_name
    st.session_state["bo_party_type"]       = _party_type
    st.session_state["bo_billing_category"] = _bill_cat
    st.session_state["bo_cart"]             = list(_cart_snap)  # restored cart, new order ID
    st.session_state["bo_tab"]              = 1    # go straight to cart review

    # Persist source for audit trail
    if _src_ono:
        st.session_state["_bo_source_order_no"] = _src_ono

    # Mark duplicate mode
    st.session_state["_bo_duplicate_mode"] = True


def _rx_text_input(container, label, key, default="", placeholder="0.00"):
    raw = st.session_state.get(key, default)
    return container.text_input(label, value="" if raw is None else str(raw), key=key,
                                placeholder=placeholder)


def _parse_rx_value(raw, *, is_axis=False):
    txt = str(raw or "").strip()
    if txt in ("", "+", "-", ".", "+.", "-."):
        return None
    try:
        val = int(float(txt)) if is_axis else float(txt)
    except Exception:
        return None
    if is_axis:
        return val if 0 <= val <= 180 and val != 0 else None
    return val if abs(val) > 0.0001 else None


def _oph_spec_pair_price(spec: dict, party_type: str = "WHOLESALE") -> float:
    if not spec:
        return 0.0
    price = spec.get("price") or {}
    keys = ("srp", "mrp", "selling", "selling_price", "price") if party_type == "RETAIL" else (
        "wlp", "selling", "selling_price", "price", "srp", "mrp"
    )
    for key in keys:
        try:
            val = float(price.get(key) or 0)
        except Exception:
            val = 0.0
        if val > 0:
            return val
    try:
        return float(spec.get("pair_price") or spec.get("selling_price") or 0)
    except Exception:
        return 0.0


def _oph_spec_per_lens_price(spec: dict, party_type: str = "WHOLESALE") -> float:
    pair = _oph_spec_pair_price(spec, party_type)
    return round(pair / 2.0, 2) if pair > 0 else 0.0


def _oph_lens_params(spec: dict) -> dict:
    if not spec:
        return {}
    return {
        "index": spec.get("index"),
        "index_value": spec.get("index"),
        "coating": spec.get("coating"),
        "treatment": spec.get("treatment"),
        "manufacturing_route": "STOCK",
        "order_source": "BULK",
    }


# ── Stock helpers ─────────────────────────────────────────────────────────────

def _get_stock(product_id: str, sph=None, cyl=None, axis=None,
               add_power=None, eye_side: str = "B", coating: str = None) -> dict:
    """
    Get combined available stock for a product+power+eye combination.
    Returns {available, r_stock_id, l_stock_id, b_stock_id, is_split}
    is_split = True when B eye should be split into R+L rows.
    Cached in session_state for performance.
    """
    # Normalize early — maps O/OTHER/None → B so all downstream logic is clean
    eye_side = normalize_eye_side(eye_side, service_aware=False)
    cache_key = f"bo_stock_{product_id}_{sph or 0}_{cyl or 0}_{axis or 0}_{add_power or 0}_{eye_side}_{coating or ''}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    p = {
        "pid": product_id,
        "sph": sph  or 0,
        "cyl": cyl  or 0,
        "ax":  axis or 0,
        "add": add_power or 0,
        "coat": coating or "",
        "coats": (
            [str(coating or "").lower(), "murk vision"]
            if (add_power or 0) and str(coating or "").strip().lower() == "green"
            else [str(coating or "").lower()]
        ),
    }

    # Sum stock across ALL batches for each eye side — only real batched rows (qty > 0)
    def _sum_stock(eye_values):
        rows = _rq("""
            SELECT COALESCE(SUM(GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0))), 0) AS total_qty,
                   COUNT(*) AS batch_count
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND COALESCE(sph,0)=%(sph)s
              AND (
                    COALESCE(cyl,0)=%(cyl)s
                    OR (%(add)s <> 0 AND COALESCE(cyl,0)=0)
                  )
              AND (
                    COALESCE(axis,0)=0
                    OR ABS(COALESCE(axis,0) - %(ax)s) <= 1
                  )
              AND COALESCE(add_power,0)=%(add)s
              AND (%(coat)s = '' OR LOWER(COALESCE(coating,'')) = ANY(%(coats)s))
              AND UPPER(eye_side) = ANY(%(eyes)s)
              AND COALESCE(is_active,TRUE)=TRUE
              AND batch_no IS NOT NULL
              AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) > 0
        """, {**p, "eyes": eye_values})
        qty   = int(rows[0]["total_qty"]   if rows else 0)
        count = int(rows[0]["batch_count"] if rows else 0)
        return qty, count > 0

    r_qty, r_has = _sum_stock(['R','RIGHT'])
    l_qty, l_has = _sum_stock(['L','LEFT'])
    b_qty, b_has = _sum_stock(['B','BOTH','PAIR','MAIN',''])

    # B eye requested + separate R/L rows exist → split mode (pair)
    is_split = (eye_side.upper() == "B" and r_has and l_has
                and r_qty > 0 and l_qty > 0)

    if is_split:
        available = min(r_qty, l_qty)   # pairs = min of R and L
    elif eye_side.upper() in ("R", "RIGHT"):
        available = r_qty or b_qty
    elif eye_side.upper() in ("L", "LEFT"):
        available = l_qty or b_qty
    else:
        # B with single B row (contact lens)
        available = b_qty

    result = {
        "available":   available,
        "r_stock_id":  None,   # summed across batches — no single id
        "r_qty":       r_qty,
        "l_stock_id":  None,
        "l_qty":       l_qty,
        "b_stock_id":  None,
        "b_qty":       b_qty,
        "is_split":    is_split,
    }
    st.session_state[cache_key] = result
    return result


def _price_for_party(product_id: str, party_type: str,
                     sph=None, cyl=None, axis=None, add_power=None,
                     eye_side: str = "B") -> dict:
    """Get MRP/selling_price/purchase_rate from inventory_stock for this power. Cached for performance."""
    cache_key = f"bo_price_{product_id}_{party_type}_{sph or 0}_{cyl or 0}_{axis or 0}_{add_power or 0}_{eye_side}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    p = {
        "pid": product_id,
        "sph": sph  or 0, "cyl": cyl  or 0,
        "ax":  axis or 0, "add": add_power or 0,
    }
    rows = _rq("""
        SELECT COALESCE(mrp,0)           AS mrp,
               COALESCE(selling_price,0) AS selling_price,
               COALESCE(purchase_rate,0) AS purchase_rate,
               COALESCE(gst_percent,
                   (SELECT gst_percent FROM products WHERE id=%(pid)s::uuid LIMIT 1),
                   12) AS gst_pct
        FROM inventory_stock
        WHERE product_id = %(pid)s::uuid
          AND COALESCE(sph,0)=%(sph)s AND COALESCE(cyl,0)=%(cyl)s
          AND COALESCE(add_power,0)=%(add)s
          AND COALESCE(is_active,TRUE)=TRUE
          AND batch_no IS NOT NULL
          AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) > 0
          AND COALESCE(mrp,0) > 0
        ORDER BY expiry_date ASC NULLS LAST
        LIMIT 1
    """, p)

    # Fallback 1: any batch row with price even if qty=0
    if not rows or not float(rows[0].get("mrp") or 0):
        rows = _rq("""
            SELECT COALESCE(mrp,0)           AS mrp,
                   COALESCE(selling_price,0) AS selling_price,
                   COALESCE(purchase_rate,0) AS purchase_rate,
                   COALESCE(gst_percent,
                       (SELECT gst_percent FROM products WHERE id=%(pid)s::uuid LIMIT 1),
                       12) AS gst_pct
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND COALESCE(sph,0)=%(sph)s AND COALESCE(cyl,0)=%(cyl)s
              AND COALESCE(add_power,0)=%(add)s
              AND COALESCE(is_active,TRUE)=TRUE
              AND batch_no IS NOT NULL
              AND COALESCE(mrp,0) > 0
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
        """, p)

    # Fallback 2: official DB price resolver (Price Master / Oph Specs /
    # inventory fallback). Do not read pricing from products; product master
    # does not own MRP/Selling/Purchase in this schema.
    if not rows or not float(rows[0].get("mrp") or 0):
        try:
            from modules.core.price_source_resolver import resolve_db_price

            resolved = resolve_db_price(
                product_id,
                party_type,
                product={"product_id": product_id},
                prefer_batch=True,
            )
            if resolved.get("found"):
                rows = [{
                    "mrp": resolved.get("mrp", 0),
                    "selling_price": resolved.get("selling_price", 0),
                    "purchase_rate": resolved.get("purchase_rate", 0),
                    "gst_pct": resolved.get("gst_percent", 12),
                }]
        except Exception:
            rows = []

    r = rows[0] if rows else {}
    mrp  = float(r.get("mrp") or 0)
    sp   = float(r.get("selling_price") or 0)
    gst  = float(r.get("gst_pct") or 12)

    # Retail → MRP (GST inclusive), Wholesale → selling_price (GST exclusive)
    unit_price = mrp if party_type == "RETAIL" else (sp or mrp)
    result = {"unit_price": unit_price, "mrp": mrp,
              "selling_price": sp, "gst_pct": gst}
    st.session_state[cache_key] = result
    return result


# ── FIFO batch fetcher ────────────────────────────────────────────────────────

def _get_fifo_batches(product_id: str, sph=None, cyl=None, axis=None,
                      add_power=None, eye_side: str = "B",
                      coating: str = None) -> list:
    """
    FIFO batch fetcher for billing screens.

    NOTE: batch_manager.get_batches_fifo() exists but uses pandas + DEBUG prints
    and doesn't handle R/L progressive split. This clean SQL version is intentional.
    When batch_manager is refactored (Phase 1 punch engine), this logic should move
    there and bulk_order should delegate.

    Returns list of dicts — FIFO by expiry, R/L split for progressives:
        {stock_id, batch_no, expiry_date, available_qty,
         mrp, selling_price, eye_side, is_rl_split}
    """
    # Normalize early — maps O/OTHER/None → B
    eye_side = normalize_eye_side(eye_side, service_aware=False)
    p = {
        "pid": product_id,
        "sph": sph  or 0,
        "cyl": cyl  or 0,
        "ax":  axis or 0,
        "add": add_power or 0,
        "coat": coating or "",
        "coats": (
            [str(coating or "").lower(), "murk vision"]
            if (add_power or 0) and str(coating or "").strip().lower() == "green"
            else [str(coating or "").lower()]
        ),
    }

    def _fetch(eyes: list) -> list:
        rows = _rq("""
            SELECT
                id::text                            AS stock_id,
                batch_no,
                expiry_date::text                   AS expiry_date,
                GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) AS available_qty,
                COALESCE(mrp, 0)                    AS mrp,
                COALESCE(selling_price, 0)          AS selling_price,
                COALESCE(purchase_rate, 0)          AS purchase_rate,
                eye_side,
                COALESCE(location, '')              AS location
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND COALESCE(sph,       0) = %(sph)s
              AND (
                    COALESCE(cyl, 0) = %(cyl)s
                    OR (%(add)s <> 0 AND COALESCE(cyl, 0) = 0)
                  )
              AND (
                    COALESCE(axis, 0) = 0
                    OR ABS(COALESCE(axis, 0) - %(ax)s) <= 1
                  )
              AND COALESCE(add_power, 0) = %(add)s
              AND (%(coat)s = '' OR LOWER(COALESCE(coating,'')) = ANY(%(coats)s))
              AND UPPER(eye_side) = ANY(%(eyes)s)
              AND COALESCE(is_active, TRUE) = TRUE
              AND batch_no IS NOT NULL
              AND GREATEST(0, COALESCE(quantity, 0) - COALESCE(allocated_qty, 0)) > 0
            ORDER BY expiry_date ASC NULLS LAST, updated_at ASC
        """, {**p, "eyes": eyes})
        return rows or []

    eye_upper = (eye_side or "B").upper()

    # Check if B eye should split into R+L
    r_batches = _fetch(["R", "RIGHT"])
    l_batches = _fetch(["L", "LEFT"])
    b_batches = _fetch(["B", "BOTH", "PAIR", "MAIN", ""])

    has_rl = bool(r_batches and l_batches)

    if eye_upper == "B" and has_rl:
        # Progressive/bifocal split — pair batches R+L by expiry (FIFO)
        # Return as paired tuples: one entry per R batch, matching L deducted
        paired = []
        l_pool = list(l_batches)  # mutable copy
        for r in r_batches:
            if not l_pool:
                break
            l = l_pool[0]
            avail = min(int(r["available_qty"]), int(l["available_qty"]))
            if avail <= 0:
                l_pool.pop(0)
                continue
            paired.append({
                "stock_id":    r["stock_id"],
                "l_stock_id":  l["stock_id"],
                "batch_no":    r["batch_no"],
                "l_batch_no":  l["batch_no"],
                "expiry_date": r["expiry_date"] or l["expiry_date"],
                "available_qty": avail,
                "mrp":         r["mrp"],
                "selling_price": r["selling_price"],
                "eye_side":    "B",
                "is_rl_split": True,
                "location":    r["location"],
            })
            # Consume from l_pool
            l["available_qty"] = int(l["available_qty"]) - avail
            if l["available_qty"] <= 0:
                l_pool.pop(0)
        return paired

    elif eye_upper in ("R", "RIGHT"):
        rows = r_batches or b_batches
        return [dict(b, is_rl_split=False) for b in rows]
    elif eye_upper in ("L", "LEFT"):
        rows = l_batches or b_batches
        return [dict(b, is_rl_split=False) for b in rows]
    else:
        # B eye with single B stock rows (contact lenses)
        return [dict(b, is_rl_split=False) for b in b_batches]


def _allocate_fifo(batches: list, required_qty: int) -> list:
    """
    Allocate required_qty across batches in FIFO order.
    Returns list of batches with allocated_qty set.
    Each batch with allocated_qty > 0 becomes a separate order line.
    """
    result = []
    remaining = required_qty
    for b in batches:
        if remaining <= 0:
            break
        avail = int(b.get("available_qty", 0))
        if avail <= 0:
            continue
        alloc = min(avail, remaining)
        remaining -= alloc
        result.append({**b, "allocated_qty": alloc})
    return result


# ── Order number ──────────────────────────────────────────────────────────────

def _next_order_no(party_type: str) -> str:
    series = "RETAIL" if party_type == "RETAIL" else "WHOLESALE"
    try:
        from modules.db.order_number_registry import (
            next_order_number, ensure_registry, format_doc_number
        )
        from modules.sql_adapter import get_transaction_connection, close_connection
        conn = get_transaction_connection()
        cur  = conn.cursor()
        try:
            ensure_registry(cur)
            seq, display = next_order_number(cur, series=series)
            conn.commit()
            # Format as R/2526/0001 or W/2526/0001
            return format_doc_number(series, seq)
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            close_connection(conn)
    except Exception:
        import datetime as _dt
        prefix = "R" if party_type == "RETAIL" else "W"
        return f"{prefix}-CS-{_dt.datetime.now().strftime('%H%M%S')}"


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — PARTY SELECTOR
# ══════════════════════════════════════════════════════════════════════════════

def _render_party_stage():
    _render_party_stage_mode(st.session_state.get("bo_party_type", "RETAIL"))


def _render_party_stage_mode(mode: str):
    """
    Party selector filtered by mode:
    RETAIL    → patients + retail walk-in parties
    WHOLESALE → dealers / distributors
    """
    is_retail = (mode == "RETAIL")
    st.markdown(f"#### 🏢 Select {'Patient / Customer' if is_retail else 'Wholesale Party'}")

    # ── Step 1: Load ALL active parties (no party_type filter) ──────────
    # Use the simplest possible query first — no billing_category, no UNION
    # to isolate any DB issues. Filter by mode after loading.
    parties = []
    _load_error = None

    try:
        from modules.sql_adapter import run_query as _direct_rq
        all_parties = _direct_rq("""
            SELECT id::text AS id,
                   party_name,
                   COALESCE(party_type,'') AS party_type,
                   COALESCE(mobile,'')     AS mobile,
                   COALESCE(payment_mode,'ON_COMPLETION') AS billing_category,
                   COALESCE(credit_limit,0) AS credit_limit,
                   COALESCE(credit_days,0)  AS credit_days,
                   'PARTY'::text AS source
            FROM parties
            WHERE COALESCE(is_active,TRUE) = TRUE
            ORDER BY party_name
        """) or []

        # Try to also load patients (retail only)
        patients_list = []
        if is_retail:
            try:
                patients_list = _direct_rq("""
                    SELECT id::text AS id,
                           master_name AS party_name,
                           'PATIENT'   AS party_type,
                           COALESCE(mobile,'') AS mobile,
                           'ADVANCE_BALANCE'   AS billing_category,
                           0::numeric AS credit_limit,
                           0::integer AS credit_days,
                           'PATIENT'::text AS source
                    FROM patients
                    ORDER BY master_name
                """) or []
            except Exception as _pe:
                _load_error = f"Patients query failed: {_pe}"

    except Exception as e:
        _load_error = str(e)
        all_parties = []
        patients_list = []

    if _load_error:
        st.error(f"DB error: {_load_error}")
        return

    # ── Step 2: Filter by mode ────────────────────────────────────────
    _supplier_types = {'SUPPLIER','VENDOR','LAB'}
    _wholesale_types = {'WHOLESALE','DISTRIBUTOR','DEALER','TRADER','B2B'}

    if is_retail:
        # Retail: parties that are NOT supplier/vendor/lab/wholesale + all patients
        retail_parties = [p for p in all_parties
                          if p["party_type"].upper() not in
                          (_supplier_types | _wholesale_types)]
        parties = retail_parties + patients_list
    else:
        # Wholesale: parties tagged as wholesale types
        parties = [p for p in all_parties
                   if p["party_type"].upper() in _wholesale_types]
        if not parties:
            # Fallback: all non-supplier parties
            parties = [p for p in all_parties
                       if p["party_type"].upper() not in _supplier_types]

    # Try to upgrade billing_category from new column if available
    try:
        from modules.sql_adapter import run_query as _bc_rq
        bc_rows = _bc_rq("""
            SELECT id::text, billing_category FROM parties
            WHERE billing_category IS NOT NULL
        """) or []
        bc_map = {r["id"]: r["billing_category"] for r in bc_rows}
        for p in parties:
            if p["id"] in bc_map:
                p["billing_category"] = bc_map[p["id"]]
    except Exception:
        pass  # billing_category column not yet migrated

    if not parties:
        pt_list = list({p["party_type"] for p in all_parties})[:8]
        st.warning(
            f"No {'patients/customers' if is_retail else 'wholesale parties'} found. "
            f"Party types in DB: {pt_list}"
        )
        return

    search = st.text_input("🔍 Search name or mobile",
                            key=f"bo_party_search_{mode}",
                            placeholder="Type to filter…")
    filtered = [p for p in parties if
                not search or search.lower() in p["party_name"].lower()
                or search in p.get("mobile","")
               ] if search else parties

    if not filtered:
        st.info("No match found.")
        return

    def _lbl(p):
        icon = "👤" if p.get("source") == "PATIENT" else "🏪"
        mob  = f" · {p['mobile']}" if p.get("mobile") else ""
        return f"{icon} {p['party_name']}{mob}"

    idx = st.selectbox("Select", range(len(filtered)),
                       format_func=lambda i: _lbl(filtered[i]),
                       key=f"bo_party_sel_{mode}")
    sel = filtered[idx]

    color   = "#4ade80" if is_retail else "#60a5fa"
    bg      = "#0d2818"  if is_retail else "#0a1628"
    border  = "#22c55e"  if is_retail else "#3b82f6"
    badge   = ("Retail — Payment before invoice"
               if is_retail else "Wholesale — Credit allowed")
    bcol    = "#60a5fa" if is_retail else "#a78bfa"
    bbg     = "#1e3a5f" if is_retail else "#1a0a2e"

    # Billing category badge
    _bc = sel.get("billing_category") or ("ADVANCE_BALANCE" if is_retail else "ON_COMPLETION")
    _bc_labels = {
        "FULL_ADVANCE":    ("💵 Full Advance",        "#ef4444"),
        "ADVANCE_BALANCE": ("🛍️ Advance + Balance",  "#8b5cf6"),
        "PRE_PAYMENT":     ("💳 Pre-Payment",         "#f59e0b"),
        "ON_COMPLETION":   ("📦 On Completion",       "#10b981"),
        "ON_ACCOUNT":      ("📒 On Account",          "#3b82f6"),
    }
    _bc_lbl, _bc_col = _bc_labels.get(_bc, (_bc, "#6b7280"))
    _cl = float(sel.get("credit_limit") or 0)
    _cd = int(sel.get("credit_days") or 0)

    st.markdown(
        f"<div style='background:{bg};border:1px solid {border};"
        f"border-radius:8px;padding:8px 14px;margin-top:8px'>"
        f"<b style='color:{color}'>{sel['party_name']}</b>"
        f" <span style='color:#6b7280'>{sel['party_type']}</span>"
        f" <span style='background:{bbg};color:{bcol};padding:2px 8px;"
        f"border-radius:8px;font-size:0.75rem'>{badge}</span>"
        f" <span style='background:#1c1c2e;color:{_bc_col};padding:2px 8px;"
        f"border-radius:8px;font-size:0.75rem;margin-left:4px'>{_bc_lbl}</span>"
        + (f"<span style='color:#6b7280;font-size:0.72rem;margin-left:8px'>"
           f"Credit: ₹{_cl:,.0f} / {_cd}d</span>" if _cl > 0 else "")
        + "</div>",
        unsafe_allow_html=True
    )

    _enter_to_click()
    if st.button("✅ Confirm  [Enter]", type="primary",
                 use_container_width=True,
                 key=f"bo_confirm_{mode}"):
        st.session_state.bo_party_id          = sel["id"]
        st.session_state.bo_party_name        = sel["party_name"]
        st.session_state.bo_party_type        = mode
        st.session_state.bo_billing_category  = sel.get("billing_category") or (
            "ADVANCE_BALANCE" if mode == "RETAIL" else "ON_COMPLETION"
        )
        st.session_state.bo_credit_limit      = float(sel.get("credit_limit") or 0)
        st.session_state.bo_credit_days       = int(sel.get("credit_days") or 30)
        st.session_state.bo_cart              = []
        st.session_state.bo_tab               = 1
        st.rerun()


def _render_cart_stage():
    party_type = st.session_state.bo_party_type
    party_name = st.session_state.bo_party_name

    # ── Duplicate mode banner ─────────────────────────────────────────────
    if st.session_state.get("_bo_duplicate_mode"):
        _n = len(st.session_state.get("bo_cart",[]))
        st.markdown(
            f"<div style='background:#1a1a2e;border:2px solid #6366f1;"
            f"border-radius:8px;padding:8px 14px;margin-bottom:8px'>"
            f"<b style='color:#a5b4fc'>📄 Duplicate Mode</b>"
            f"<span style='color:#64748b;font-size:0.75rem;margin-left:10px'>"
            f"Cart loaded with {_n} line(s) from previous order. "
            f"Edit quantities or remove items before placing.</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        f"<div style='background:#0f172a;border:1px solid #1e293b;"
        f"border-radius:8px;padding:6px 14px;margin-bottom:10px'>"
        f"Party: <b style='color:#60a5fa'>{party_name}</b>"
        f" · <span style='color:#94a3b8'>{party_type}</span>"
        f"</div>", unsafe_allow_html=True
    )

    # ── Product search / scan ─────────────────────────────────────────
    st.markdown("#### ➕ Add Product")
    st.markdown(
        "<div style='background:#0a1628;border:1px solid #1e3a5f;border-radius:6px;"
        "padding:5px 12px;margin-bottom:8px;font-size:.65rem;color:#475569;"
        "display:flex;gap:16px'>"
        "<span>⌨️ <b style='color:#60a5fa'>Enter</b> = primary action</span>"
        "<span><b style='color:#60a5fa'>Tab</b> = next field</span>"
        "<span><b style='color:#60a5fa'>Scan</b> = auto-add to cart</span>"
        "</div>",
        unsafe_allow_html=True
    )

    # Auto-focus scan input every time cart stage renders
    _autofocus_scan()

    scan_col, clear_col = st.columns([4, 1])
    with scan_col:
        scan_val = st.text_input(
            "📷 Scan Code / Item Code or search product",
            key="bo_scan",
            placeholder="Scan Code / Item Code → auto-adds  |  Type name → select below",
            label_visibility="collapsed",
        )
        st.caption("Scanning rule: stock scan codes are saved in inventory_stock.item_code. Batch No is not used for frame scanning.")
    with clear_col:
        if st.button("✕", key="bo_scan_clear", use_container_width=True):
            st.session_state.pop("bo_scan", None)
            st.rerun()

    # Barcode scan — direct add
    if scan_val and scan_val.strip():
        hit = _rq("""
            SELECT
                p.id::text AS product_id, p.product_name,
                COALESCE(p.brand,'') AS brand,
                COALESCE(p.main_group,'') AS main_group,
                COALESCE(p.gst_percent,12) AS gst_pct,
                s.id::text AS stock_id,
                COALESCE(s.sph,0) AS sph, COALESCE(s.cyl,0) AS cyl,
                COALESCE(s.axis,0) AS axis, COALESCE(s.add_power,0) AS add_power,
                COALESCE(s.eye_side,'B') AS eye_side,
                COALESCE(s.mrp,0) AS mrp,
                COALESCE(s.selling_price,0) AS selling_price,
                GREATEST(0, COALESCE(s.quantity,0) - COALESCE(s.allocated_qty,0)) AS available
            FROM inventory_stock s
            JOIN products p ON p.id = s.product_id
            WHERE (UPPER(TRIM(COALESCE(s.barcode,''))) = UPPER(TRIM(%s))
                OR UPPER(TRIM(COALESCE(s.product_barcode,''))) = UPPER(TRIM(%s))
                OR UPPER(TRIM(COALESCE(s.item_code,''))) = UPPER(TRIM(%s)))
              AND COALESCE(s.is_active,TRUE)=TRUE
              AND GREATEST(0, COALESCE(s.quantity,0) - COALESCE(s.allocated_qty,0)) > 0
            ORDER BY s.expiry_date ASC NULLS LAST
            LIMIT 1
        """, (scan_val.strip(), scan_val.strip(), scan_val.strip()))

        if hit:
            h = hit[0]
            unit_price = float(h["mrp"]) if party_type == "RETAIL" \
                         else float(h["selling_price"] or h["mrp"])
            _add_to_cart(
                product_id  = h["product_id"],
                product_name= h["product_name"],
                brand       = h["brand"],
                main_group  = h["main_group"],
                sph         = float(h["sph"]) if h["sph"] else None,
                cyl         = float(h["cyl"]) if h["cyl"] else None,
                axis        = int(h["axis"]) if h["axis"] else None,
                add_power   = float(h["add_power"]) if h["add_power"] else None,
                eye_side    = h["eye_side"],
                unit_price  = unit_price,
                gst_pct     = float(h["gst_pct"]),
                qty         = 1,
                final_pcs   = 1,
                party_type  = party_type,
            )
            st.session_state.pop("bo_scan", None)
            st.rerun()
        else:
            st.warning(f"Barcode '{scan_val}' not found. Use product search below.")

    # Manual product search — cached for performance
    cache_key = "bo_products_list"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = _rq("""
            SELECT DISTINCT p.id::text AS product_id, p.product_name,
                   COALESCE(p.brand,'') AS brand,
                   COALESCE(p.main_group,'') AS main_group,
                   COALESCE(p.gst_percent,12) AS gst_pct

            FROM products p

            ORDER BY p.product_name
        """)
    products = st.session_state[cache_key]

    if products:
        prod_search = st.text_input("🔍 Filter products",
                                     key="bo_prod_filter",
                                     placeholder="Type to filter…",
                                     label_visibility="collapsed")
        filtered_p = [p for p in products if
                      not prod_search or prod_search.lower()
                      in f"{p['product_name']} {p['brand']}".lower()]

        if filtered_p:
            prod_labels = [f"{p['product_name']} · {p['brand']} · {p['main_group']}"
                           for p in filtered_p]
            col_p, col_eye = st.columns([4, 1])
            with col_p:
                prod_idx = st.selectbox("Product", range(len(filtered_p)),
                                         format_func=lambda i: prod_labels[i],
                                         key="bo_prod_sel")
            with col_eye:
                eye = st.selectbox("Eye", ["B","R","L","—"],
                                    key="bo_eye_sel",
                                    help="B=Both/Pair, R=Right, L=Left, —=No eye")

            sel_p = filtered_p[prod_idx]
            eye_side = None if eye == "—" else eye

            # Power inputs
            mg_lower = sel_p["main_group"].lower()
            needs_power = ("lens" in mg_lower or "ophthalmic" in mg_lower
                           or "spectacle" in mg_lower or "contact" in mg_lower)

            sph = cyl = axis = add_power = None
            if needs_power:
                # ── Same power both eyes shortcut ─────────────────────────
                # Bulk uses single power entry for both eyes (is_pair=True).
                # Checkbox label reflects this — no separate L field to mirror.
                st.checkbox(
                    "↔ Same power both eyes",
                    key="bo_copy_same_rl",
                    value=True,
                    disabled=True,
                    help="Bulk order uses the same power entry for both eyes automatically.",
                )
                pc1, pc2, pc3, pc4 = st.columns(4)
                sph_v = _rx_text_input(pc1, "SPH", "bo_sph", placeholder="")
                cyl_v = _rx_text_input(pc2, "CYL", "bo_cyl", placeholder="")
                ax_v  = _rx_text_input(pc3, "AXIS", "bo_ax", placeholder="")
                add_v = _rx_text_input(pc4, "ADD", "bo_add", placeholder="")
                sph       = _parse_rx_value(sph_v)
                cyl       = _parse_rx_value(cyl_v)
                axis      = _parse_rx_value(ax_v, is_axis=True)
                add_power = _parse_rx_value(add_v)

            # ── Ophthalmic spec selector (index + coating) ────────────────────
            _bo_is_oph = (
                "ophthalmic" in mg_lower
                or "spectacle" in mg_lower
                or "single vision" in mg_lower
                or "progressive" in mg_lower
            ) and "contact" not in mg_lower
            _bo_oph_spec = None
            if _bo_is_oph and _HAS_OPH:
                _bo_oph_spec = _oph_sel(
                    product_id   = str(sel_p.get("product_id","")),
                    product_name = str(sel_p.get("product_name","")),
                    rx_r         = {"sph": sph, "cyl": cyl, "axis": axis, "add": add_power},
                    rx_l         = {"sph": sph, "cyl": cyl, "axis": axis, "add": add_power},
                    order_type   = party_type,
                    key_prefix   = f"bo_{str(sel_p.get('product_id',''))[:8]}",
                )
            _bo_oph_complete = bool(_bo_oph_spec and _bo_oph_spec.get("complete"))
            _bo_oph_coating = (_bo_oph_spec or {}).get("coating") if _bo_oph_complete else None
            _bo_lens_params = _oph_lens_params(_bo_oph_spec) if _bo_oph_complete else {}

            # ── Load product + delegate all price/qty to governor ──────
            prod_detail = _rq("""
                SELECT id::text AS product_id, unit, box_size, allow_loose,
                       COALESCE(gst_percent,12) AS gst_pct
                FROM products WHERE id=%s::uuid LIMIT 1
            """, (sel_p["product_id"],))
            prod_dict = prod_detail[0] if prod_detail else {}

            from modules.core.price_qty_governor import (
                resolve_price, normalize_box_total, compute_line_gst,
                detect_qty_mode, box_qty_label, reverse_qty, pcs_to_display,
                QTY_MODE_BOX_ONLY, QTY_MODE_FLEX,
            )

            qty_mode    = detect_qty_mode(prod_dict)
            box_size    = max(1, int(prod_dict.get("box_size") or 1))
            allow_loose = prod_dict.get("allow_loose") in (True,"t","true","True",1,"1")
            gst_pct_val = float(sel_p.get("gst_pct") or prod_dict.get("gst_pct") or 12)
            _is_cl      = "contact" in sel_p["main_group"].lower()

            # Effective box_size for CL (infer from product name if prod has 1)
            _eff_bs = box_size
            if _is_cl and box_size == 1:
                for kw, sz in [("6P",6),("SIX",6),("3P",3),("THREE",3),("1P",1)]:
                    if kw in sel_p["product_name"].upper():
                        _eff_bs = sz
                        break

            # ── FIFO batch availability ───────────────────────────────
            fifo_batches = _get_fifo_batches(
                sel_p["product_id"], sph, cyl, axis, add_power, eye_side or "B",
                coating=_bo_oph_coating if _bo_is_oph else None,
            )
            avail = sum(int(b.get("available_qty", 0)) for b in fifo_batches)

            # ── Stock indicator — DB stores pcs, display as boxes ────
            if avail == 0:
                # ── Range check first ─────────────────────────────────────
                _in_range_bo = True
                try:
                    from modules.power_intelligence_ui import render_range_check as _rc_bo
                    _in_range_bo = _rc_bo(
                        product_id   = str(sel_p.get("product_id", "")),
                        product_name = str(sel_p.get("product_name", "")),
                        sph  = float(sph or 0),
                        cyl  = float(cyl or 0),
                        axis = int(axis or 0),
                        is_colour=False,
                        eye="",
                    )
                except Exception:
                    _in_range_bo = True
                if _in_range_bo:
                    st.error("❌ No stock. Add batch via Product & Inventory → Batch Manager.")
                # ── Power Intelligence — show alternatives ─────────────────
                if _pi_panel:
                    try:
                        _pi_panel(
                            sph=float(sph or 0),
                            cyl=float(cyl or 0),
                            axis=int(axis or 0),
                            add_power=float(add_power or 0),
                            selected_product=sel_p.get("product_name", ""),
                            eye="",
                            product_id=str(sel_p.get("product_id","") or ""),
                            is_colour=False,
                        )
                    except Exception:
                        pass
            else:
                from modules.core.price_qty_governor import pcs_to_display
                _stk_lbl = pcs_to_display(avail, prod_dict)
                _low = (avail // max(1, _eff_bs)) <= 3 if _eff_bs > 1 else avail <= 3
                if _low:
                    st.warning(f"⚠️ Low stock — {_stk_lbl} across {len(fifo_batches)} batch(es)")
                else:
                    st.success(f"✅ {_stk_lbl} available across {len(fifo_batches)} batch(es)")

            # ── Batch breakdown panel ─────────────────────────────────
            if fifo_batches and avail > 0:
                with st.expander(f"📦 {len(fifo_batches)} batch(es)", expanded=False):
                    for b in fifo_batches:
                        bq  = int(b.get("available_qty", 0))
                        bn  = b.get("batch_no", "—")
                        exp = (b.get("expiry_date") or "")[:10]
                        mrp = float(b.get("mrp") or 0)
                        sp  = float(b.get("selling_price") or 0)
                        _pr = mrp if party_type == "RETAIL" else (sp or mrp)
                        c1, c2, c3, c4 = st.columns([2,2,2,1])
                        c1.markdown(f"**{bn}**")
                        c2.markdown(f"Exp: {exp or '—'}")
                        c3.markdown(f"Avail: **{pcs_to_display(bq, prod_dict)}**")
                        c4.markdown(f"₹{_pr:,.2f}")

            # Price — governor resolves correct field for order_type
            _price_row = fifo_batches[0] if fifo_batches else {}
            _raw_price = resolve_price(_price_row, party_type)
            if _bo_is_oph and _bo_oph_complete:
                _oph_ppc = _oph_spec_per_lens_price(_bo_oph_spec, party_type)
                if _oph_ppc > 0:
                    _raw_price = _raw_price or _oph_ppc

            # ── Price Governor: validate + dropdown if old-price stock ────────
            if _render_price and prod_dict.get('product_id'):
                try:
                    _pg_sel = _render_price(
                        product_id  = str(prod_dict['product_id']),
                        party_type  = party_type,
                        batch_purchase_rate = float((_price_row or {}).get('purchase_rate') or 0) or None,
                        key_prefix  = f"bo_price_{prod_dict.get('product_id','')}",
                    )
                    if _pg_sel and not _pg_sel.get('valid', True):
                        st.stop()
                    if _pg_sel:
                        if _pg_sel.get('mrp'):      prod_dict['mrp']           = _pg_sel['mrp']
                        if _pg_sel.get('selling_price'): prod_dict['selling_price'] = _pg_sel['selling_price']
                        _raw_price = _pg_sel.get('selling_price') or _raw_price
                except Exception:
                    pass

            prices = {
                "raw": _raw_price,
                "mrp": float(_price_row.get("mrp") or 0),
                "sp":  float(_price_row.get("selling_price") or 0),
            }
            if _bo_is_oph and _bo_oph_complete:
                _oph_ppc = _oph_spec_per_lens_price(_bo_oph_spec, party_type)
                if _oph_ppc > 0:
                    if party_type == "RETAIL":
                        prices["mrp"] = prices["mrp"] or _oph_ppc
                    else:
                        prices["sp"] = prices["sp"] or _oph_ppc
                    prices["raw"] = prices["raw"] or _oph_ppc

            # ── Price version selector ─────────────────────────────────────
            if _price_selector:
                try:
                    _pv = _price_selector(
                        product_id=str(sel_p.get("product_id", "")),
                        product_name=sel_p.get("product_name", ""),
                        party_type=party_type,
                        key_prefix="bo",
                    )
                    if _pv and _pv.get('mrp'):
                        prices["mrp"] = _pv['mrp']
                        prices["sp"]  = _pv['selling_price']
                        prices["raw"] = _pv['selling_price'] if party_type != "RETAIL" else _pv['mrp']
                except Exception:
                    pass

            # ── Qty inputs ────────────────────────────────────────────
            if avail > 0:
                st.markdown("**Quantity**")
                _box_lbl = "Boxes" if not _is_cl else (
                    next((f"Boxes ({kw})" for kw in ["6P","3P","1P","SIX","THREE"]
                          if kw in sel_p["product_name"].upper()), "Boxes")
                )
                _has_box    = qty_mode in (QTY_MODE_BOX_ONLY, QTY_MODE_FLEX)
                # DB stores pcs — convert to boxes for max
                _avail_rev  = reverse_qty(avail, prod_dict)
                _max_boxes  = _avail_rev["box"] if _has_box else avail
                _avail_loose= _avail_rev["pcs"]   # leftover loose pcs in stock

                # All products: DB stores pcs universally
                # UI shows boxes+loose via detect_qty_mode + reverse_qty
                if _has_box and allow_loose:
                    qc1, qc2, qc3 = st.columns(3)
                    box_qty_inp = qc1.number_input(
                        _box_lbl, min_value=0, value=1,
                        max_value=max(1, _max_boxes), key="bo_box_qty"
                    )
                    loose_inp = qc2.number_input(
                        f"Loose Pcs (max {_eff_bs - 1})",
                        min_value=0, value=0,
                        max_value=max(0, _eff_bs - 1), key="bo_loose_qty"
                    )
                    final_pcs = box_qty_inp * _eff_bs + loose_inp
                    qc3.metric("Total", box_qty_label(box_qty_inp, loose_inp, _eff_bs))
                elif _has_box:
                    qc1, qc2 = st.columns(2)
                    if int(st.session_state.get("bo_box_qty", 0) or 0) <= 0:
                        st.session_state["bo_box_qty"] = 1
                    box_qty_inp = qc1.number_input(
                        _box_lbl, min_value=1, value=1,
                        max_value=max(1, _max_boxes), key="bo_box_qty"
                    )
                    loose_inp = 0
                    final_pcs = box_qty_inp * _eff_bs   # always pcs internally
                    qc2.metric("Total", box_qty_label(box_qty_inp, 0, _eff_bs))
                else:
                    qc1, _ = st.columns(2)
                    if int(st.session_state.get("bo_qty", 0) or 0) <= 0:
                        st.session_state["bo_qty"] = 1
                    final_pcs = qc1.number_input(
                        "Qty (pcs)", min_value=1, max_value=max(1, avail),
                        value=1, key="bo_qty"
                    )
                    box_qty_inp = 0
                    loose_inp   = final_pcs

                # ── FIFO allocation preview ───────────────────────────
                if final_pcs > 0:
                    allocated   = _allocate_fifo(fifo_batches, final_pcs)
                    total_alloc = sum(a["allocated_qty"] for a in allocated)
                    if total_alloc < final_pcs:
                        st.error(f"❌ Only {total_alloc} available — reduce qty.")
                    elif len(allocated) > 1:
                        lines_txt = []
                        for a in allocated:
                            aq = int(a["allocated_qty"])
                            lines_txt.append(
                                f"**{a['batch_no']}** → {pcs_to_display(aq, prod_dict)}"
                                f" exp:{(a.get('expiry_date') or '')[:10]}"
                            )
                        st.info("📋 FIFO: " + " · ".join(lines_txt))

                # ── Price input ───────────────────────────────────────
                col_price, col_add = st.columns([2, 1])
                with col_price:
                    _gst_lbl   = "GST incl." if party_type == "RETAIL" else "ex-GST"
                    _def_price = float(prices["mrp"] if party_type == "RETAIL"
                                       else (prices["sp"] or prices["mrp"])) or _raw_price

                    if _has_box:
                        raw_price = st.number_input(
                            f"Price ₹/box ({_eff_bs} pcs) · {_gst_lbl}",
                            min_value=0.0, step=0.50,
                            value=float(_def_price), key="bo_price"
                        )
                        _ppc = round(raw_price / _eff_bs, 2) if _eff_bs > 1 else raw_price
                        _total_pcs = final_pcs if not _is_cl else final_pcs * _eff_bs
                        _lt = normalize_box_total(raw_price, _total_pcs, {"box_size": _eff_bs})
                        st.caption(
                            f"₹{_ppc:.2f}/pcs · "
                            f"{box_qty_label(box_qty_inp, loose_inp, _eff_bs)} = ₹{_lt:,.2f}"
                        )
                    else:
                        raw_price = st.number_input(
                            f"Price ₹/pcs · {_gst_lbl}",
                            min_value=0.0, step=0.50,
                            value=float(_def_price), key="bo_price"
                        )
                        _ppc = raw_price
                        if final_pcs > 0:
                            st.caption(f"₹{raw_price:.2f}/pcs × {final_pcs} = ₹{raw_price * final_pcs:,.2f}")

                # Enter key → Add to Cart
                _enter_to_click()
                with col_add:
                    st.write("")
                    st.write("")
                    if st.button("➕ Add  [Enter]", type="primary",
                                 use_container_width=True, key="bo_add_btn",
                                 disabled=(final_pcs == 0 or avail == 0)):
                        allocated   = _allocate_fifo(fifo_batches, final_pcs)
                        total_alloc = sum(a["allocated_qty"] for a in allocated)
                        if total_alloc < final_pcs:
                            st.error("❌ Insufficient stock.")
                        else:
                            for a in allocated:
                                aq = int(a["allocated_qty"])   # always in pcs
                                if aq <= 0:
                                    continue
                                # pcs → boxes for display
                                bq_a = aq // max(1, _eff_bs)
                                lp_a = aq %  max(1, _eff_bs)
                                _total_pcs_a = aq if not _is_cl else aq * _eff_bs
                                _gst_r = compute_line_gst(
                                    _ppc, _total_pcs_a, gst_pct_val, party_type
                                )
                                _add_to_cart(
                                    product_id   = sel_p["product_id"],
                                    product_name = (
                                        _oph_name(sel_p["product_name"], _bo_oph_spec)
                                        if _bo_is_oph and _bo_oph_complete and _oph_name
                                        else sel_p["product_name"]
                                    ),
                                    brand        = sel_p["brand"],
                                    main_group   = sel_p["main_group"],
                                    sph=sph, cyl=cyl, axis=axis, add_power=add_power,
                                    eye_side     = eye_side or "B",
                                    unit_price   = _ppc,
                                    box_price    = raw_price if _has_box else None,
                                    box_size     = _eff_bs,
                                    box_qty      = bq_a,
                                    loose_pcs    = lp_a,
                                    final_pcs    = aq,
                                    gst_pct      = gst_pct_val,
                                    party_type   = party_type,
                                    is_box_mode  = _has_box,
                                    batch_no     = a.get("batch_no"),
                                    stock_id     = a.get("stock_id"),
                                    l_stock_id   = a.get("l_stock_id"),
                                    is_rl_split  = a.get("is_rl_split", False),
                                    lens_params  = _bo_lens_params,
                                )
                            st.rerun()

                            st.rerun()

    # ── Service-only quick add ────────────────────────────────────────
    _render_service_quick_add()

    # ── Cart display ──────────────────────────────────────────────────
    _render_cart()


def _render_service_quick_add():
    """Add fitting/colouring/courier/other service lines to bulk billing."""
    services = _service_master_rows(
        st.session_state.get("bo_party_type", "RETAIL"),
        party_id=str(st.session_state.get("bo_party_id") or ""),
    )
    if not services:
        return
    with st.expander("🧾 Add Service — Fitting · Colouring · Courier", expanded=False):
        labels = [
            f"{s.get('service_group')} · {s.get('service_name')} · ₹{float(s.get('default_price') or 0):.0f}"
            for s in services
        ]
        idx = st.selectbox("Service", range(len(services)), format_func=lambda i: labels[i], key="bo_svc_pick")
        svc = services[int(idx)]
        group = str(svc.get("service_group") or "OTHER").upper()
        courier_provider_id = ""
        courier_provider_name = ""
        courier_rate_option_id = ""
        courier_rate_option_label = ""
        courier_parcel_size = ""
        if group == "COURIER":
            try:
                from modules.backoffice.service_master import fetch_providers as _bulk_fetch_providers
                from modules.backoffice.service_master import fetch_courier_rate_options as _bulk_fetch_courier_slabs
                courier_rows = _bulk_fetch_providers("COURIER", active_only=True) or []
            except Exception:
                courier_rows = []
                _bulk_fetch_courier_slabs = lambda *_a, **_k: []
            provider_ids = [""] + [str(p.get("id") or "") for p in courier_rows]

            def fmt_provider(pid):
                if not pid:
                    return "— Select Courier Provider —"
                p = next((x for x in courier_rows if str(x.get("id") or "") == str(pid)), {})
                return str(p.get("provider_name") or pid)

            courier_provider_id = st.selectbox(
                "Courier provider",
                provider_ids,
                format_func=fmt_provider,
                key="bo_svc_courier_provider",
            )
            provider = next((x for x in courier_rows if str(x.get("id") or "") == str(courier_provider_id)), {})
            courier_provider_name = str(provider.get("provider_name") or "")
            slab_rows = _bulk_fetch_courier_slabs(courier_provider_id, active_only=True) if courier_provider_id else []
            slab_ids = [""] + [str(s.get("id") or "") for s in slab_rows]
            slab_idx = 0
            if slab_rows:
                lowest = min(slab_rows, key=lambda s: float(s.get("charge_base") or 0))
                low_id = str(lowest.get("id") or "")
                slab_idx = slab_ids.index(low_id) if low_id in slab_ids else 0

            def fmt_slab(sid):
                if not sid:
                    return "Provider default / manual"
                s = next((x for x in slab_rows if str(x.get("id") or "") == str(sid)), {})
                code = str(s.get("parcel_size_code") or "")
                return (
                    f"{s.get('option_label') or ''}"
                    + (f" · {code}" if code else "")
                    + f" — ₹{float(s.get('charge_base') or 0):,.2f}"
                )

            courier_rate_option_id = st.selectbox(
                "Courier charge slab / parcel size",
                slab_ids,
                index=slab_idx,
                format_func=fmt_slab,
                key="bo_svc_courier_slab",
            )
            slab = next((x for x in slab_rows if str(x.get("id") or "") == str(courier_rate_option_id)), {})
            if slab:
                svc["default_price"] = float(slab.get("charge_base") or 0)
                svc["gst_percent"] = float(slab.get("gst_percent") or svc.get("gst_percent") or 18)
                courier_rate_option_label = str(slab.get("option_label") or "")
                courier_parcel_size = str(slab.get("parcel_size_code") or "")
                st.caption("Lowest courier slab is auto-selected. Change dropdown if parcel is bigger.")
        c1, c2, cq, c3 = st.columns([2, 1, 1, 1])
        desc = c1.text_input("Description", value=str(svc.get("service_name") or ""), key="bo_svc_desc")
        amt = c2.number_input("Rate / pair ₹", min_value=0.0, step=10.0,
                              value=float(svc.get("default_price") or 0), key="bo_svc_amt")
        # Auto-detect pair from bulk cart
        _bo_cart_now = st.session_state.get("bulk_order_lines") or                        st.session_state.get("bulk_cart") or []
        _bo_eyes = {str(l.get("eye_side","")).upper()[:1]
                    for l in _bo_cart_now if not l.get("is_service_line")}
        _bo_auto_qty = 1.0 if ("R" in _bo_eyes and "L" in _bo_eyes) else (
                       0.5 if _bo_eyes else 1.0)
        _bo_auto_idx = [0.5, 1.0, 1.5, 2.0, 3.0].index(_bo_auto_qty)                        if _bo_auto_qty in [0.5, 1.0, 1.5, 2.0, 3.0] else 1
        qty_factor = cq.selectbox(
            "Qty ⚠️",
            [0.5, 1.0, 1.5, 2.0, 3.0],
            index=_bo_auto_idx,
            format_func=lambda v: (
                f"{v:g} pair — 1 eye only" if v == 0.5 else
                f"{v:g} pair — both eyes" if v == 1.0 else
                f"{v:g} pair"
            ),
            key="bo_svc_qty_factor",
            help="Auto-set from cart. 0.5 pair = 1 eye only. 1 pair = both eyes.",
        )
        gst = c3.number_input("GST %", min_value=0.0, max_value=28.0, step=0.5,
                              value=float(svc.get("gst_percent") or 0), key="bo_svc_gst")
        if st.button(f"➕ Add {qty_factor:g} pair to Cart", key="bo_svc_add", type="primary", use_container_width=True):
            if amt <= 0:
                st.error("Enter amount > 0")
                return
            code = str(svc.get("service_code") or group).upper()
            prod_route = str(svc.get("production_route") or "").upper()
            mfg_route = "INHOUSE" if prod_route == "COLOURING" else ("FITTING" if prod_route == "FITTING" else "STOCK")
            base_amt = round(float(amt) * float(qty_factor), 2)
            gst_amt = round(base_amt * float(gst) / 100, 2)
            total = round(base_amt + gst_amt, 2)
            pid = _ensure_service_product_id(group, svc.get("service_name") or desc or "Service")
            st.session_state.bo_cart.append({
                "id": str(uuid.uuid4()),
                "product_id": pid,
                "product_name": desc or svc.get("service_name") or "Service Charge",
                "brand": "Service",
                "main_group": "Services",
                "sph": None, "cyl": None, "axis": None, "add_power": None,
                "eye_side": "SERVICE", "eye_label": "SERVICE", "power_str": "",
                "is_pair": False,
                "batch_no": "", "stock_id": None, "l_stock_id": None, "is_rl_split": False,
                "unit_price": base_amt,
                "box_price": None, "box_size": 1, "box_qty": 0, "loose_pcs": 1,
                "is_box_mode": False,
                "lens_params": {
                    "charge_type": group,
                    "service_type": group,
                    "service_group": group,
                    "service_code": code,
                    "service_description": desc or svc.get("service_name") or "",
                    "service_origin": "bulk_order",
                    "service_production_type": prod_route,
                    "manufacturing_route": mfg_route,
                    "batch_status": "PENDING" if prod_route else "READY",
                    "service_qty_factor": float(qty_factor),
                    "service_rate_per_pair": float(amt),
                    "courier_provider_id": courier_provider_id,
                    "courier_provider_name": courier_provider_name,
                    "courier_rate_option_id": courier_rate_option_id,
                    "courier_rate_option_label": courier_rate_option_label,
                    "courier_parcel_size": courier_parcel_size,
                },
                "base_price": base_amt,
                "gst_pct": float(gst),
                "gst_amt": gst_amt,
                "qty": 1, "final_pcs": 1, "billing_qty": 1,
                "allocated_qty": 0 if prod_route else 1,
                "line_total": total,
                "billing_total": total,
                "discount_percent": 0.0,
                "discount_amount": 0.0,
            })
            st.rerun()


def _quick_service_party_rows(mode: str) -> List[Dict]:
    is_retail = str(mode or "").upper() == "RETAIL"
    try:
        rows = _rq("""
            SELECT id::text AS id,
                   party_name,
                   COALESCE(party_type,'') AS party_type,
                   COALESCE(mobile,'') AS mobile,
                   COALESCE(doc_preference::text, '') AS doc_preference,
                   COALESCE(billing_preference, '') AS billing_preference
            FROM parties
            WHERE COALESCE(is_active, TRUE)=TRUE
            ORDER BY party_name
        """) or []
    except Exception:
        rows = _rq("""
            SELECT id::text AS id,
                   party_name,
                   COALESCE(party_type,'') AS party_type,
                   COALESCE(mobile,'') AS mobile,
                   '' AS doc_preference,
                   '' AS billing_preference
            FROM parties
            WHERE COALESCE(is_active, TRUE)=TRUE
            ORDER BY party_name
        """) or []
    supplier_types = {"SUPPLIER", "VENDOR", "LAB", "EXTERNAL_LAB"}
    wholesale_types = {"WHOLESALE", "DISTRIBUTOR", "DEALER", "TRADER", "B2B"}
    if is_retail:
        return [r for r in rows if str(r.get("party_type") or "").upper() not in supplier_types | wholesale_types]
    out = [r for r in rows if str(r.get("party_type") or "").upper() in wholesale_types]
    return out or [r for r in rows if str(r.get("party_type") or "").upper() not in supplier_types]


def _quick_service_doc_route(party: Dict) -> str:
    ptype = str(party.get("party_type") or "").upper()
    if ptype in ("PATIENT", "RETAIL", "CUSTOMER", "WALKIN", "WALK-IN"):
        return "C"
    doc_pref = str(party.get("doc_preference") or "").strip().upper()
    legacy = str(party.get("billing_preference") or "").strip().upper()
    if doc_pref == "C" or legacy == "CHALLAN":
        return "C"
    return "I"


def _create_quick_service_order(
    *,
    party: Dict,
    service: Dict,
    desc: str,
    base_amount: float,
    gst_percent: float,
    lens_params: Dict,
) -> Dict:
    party_id = str(party.get("id") or "")
    party_name = str(party.get("party_name") or "")
    party_type_raw = str(party.get("party_type") or "WHOLESALE").upper()
    order_type = "RETAIL" if party_type_raw in ("PATIENT", "RETAIL", "CUSTOMER", "WALKIN", "WALK-IN") else "WHOLESALE"
    order_id = str(uuid.uuid4())
    order_no = _next_order_no(order_type)
    group = str(service.get("service_group") or lens_params.get("charge_type") or "OTHER").upper()
    code = str(service.get("service_code") or group).upper()
    gst_amount = round(float(base_amount or 0) * float(gst_percent or 0) / 100.0, 2)
    total_amount = round(float(base_amount or 0) + gst_amount, 2)
    product_id = _ensure_service_product_id(group, desc or service.get("service_name") or "Service")
    lp = {
        "charge_type": group,
        "service_type": group,
        "service_group": group,
        "service_code": code,
        "service_description": desc,
        "service_display_name": desc,
        "display_product_name": desc,
        "service_origin": "quick_service_billing",
        "service_production_type": "",
        "manufacturing_route": "STOCK",
        "batch_status": "READY",
    }
    lp.update({k: v for k, v in (lens_params or {}).items() if v not in (None, "")})

    ok = _rw("""
        INSERT INTO orders
          (id, order_no, order_type, order_source,
           party_id, party_name, status, total_items, total_value,
           created_at, updated_at)
        VALUES
          (%(id)s::uuid, %(ono)s, %(otype)s, 'QUICK_SERVICE',
           %(pid)s::uuid, %(pname)s, 'CONFIRMED', 1, %(total)s,
           NOW(), NOW())
    """, {
        "id": order_id,
        "ono": order_no,
        "otype": order_type,
        "pid": party_id,
        "pname": party_name,
        "total": total_amount,
    })
    if not ok:
        return {"ok": False, "error": "Failed to create quick service order."}

    import json as _json_qs
    ok = _rw("""
        INSERT INTO order_lines
          (id, order_id, product_id, eye_side,
           quantity, unit_price, total_price,
           gst_percent, gst_amount,
           billing_total, status, lens_params, boxing_params,
           allocated_qty, billed_qty, is_service_line)
        VALUES
          (%(lid)s::uuid, %(oid)s::uuid, %(pid)s::uuid, 'SERVICE',
           1, %(base)s, %(total)s,
           %(gstp)s, %(gsta)s,
           %(total)s, 'PENDING', %(lp)s::jsonb, '{}',
           1, 0, TRUE)
    """, {
        "lid": str(uuid.uuid4()),
        "oid": order_id,
        "pid": product_id,
        "base": round(float(base_amount or 0), 2),
        "total": total_amount,
        "gstp": round(float(gst_percent or 0), 2),
        "gsta": gst_amount,
        "lp": _json_qs.dumps(lp),
    })
    if not ok:
        return {"ok": False, "error": "Failed to create quick service line."}
    return {
        "ok": True,
        "order_id": order_id,
        "order_no": order_no,
        "order_type": order_type,
        "base": round(float(base_amount or 0), 2),
        "gst": gst_amount,
        "total": total_amount,
    }


def _render_quick_service_billing():
    mode = st.session_state.get("bo_mode", "WHOLESALE")
    with st.expander("⚡ Quick Service Billing — missed courier / fitting / colouring / other", expanded=False):
        parties = _quick_service_party_rows(mode)
        if not parties:
            st.info("No party found for quick service billing.")
            return
        p_search = st.text_input("Search party", key=f"qsb_party_search_{mode}", placeholder="Name / mobile")
        filtered = [
            p for p in parties
            if not p_search
            or p_search.lower() in str(p.get("party_name") or "").lower()
            or p_search in str(p.get("mobile") or "")
        ]
        if not filtered:
            st.info("No matching party.")
            return
        pidx = st.selectbox(
            "Party",
            range(len(filtered)),
            format_func=lambda i: (
                f"{filtered[i].get('party_name')}"
                + (f" · {filtered[i].get('mobile')}" if filtered[i].get("mobile") else "")
                + f" · {filtered[i].get('party_type') or ''}"
            ),
            key=f"qsb_party_{mode}",
        )
        party = filtered[int(pidx)]
        route = _quick_service_doc_route(party)
        st.caption(
            "DB route: "
            + ("Challan first" if route == "C" else "Direct invoice")
            + " · no staff choice"
        )

        services = _service_master_rows(
            "RETAIL" if str(party.get("party_type") or "").upper() in ("PATIENT", "RETAIL", "CUSTOMER") else "WHOLESALE",
            party_id=str(party.get("id") or ""),
        )
        services = [s for s in services if str(s.get("service_group") or "").upper() in ("COURIER", "FITTING", "COLOURING", "OTHER", "MISC")]
        if not services:
            st.info("No active services in Service Master.")
            return
        sidx = st.selectbox(
            "Service",
            range(len(services)),
            format_func=lambda i: f"{services[i].get('service_group')} · {services[i].get('service_name')} · ₹{float(services[i].get('default_price') or 0):.0f}",
            key="qsb_service",
        )
        svc = services[int(sidx)]
        group = str(svc.get("service_group") or "OTHER").upper()

        courier_meta = {
            "courier_provider_id": "",
            "courier_provider_name": "",
            "courier_rate_option_id": "",
            "courier_rate_option_label": "",
            "courier_parcel_size": "",
        }
        if group == "COURIER":
            try:
                from modules.backoffice.service_master import fetch_providers, fetch_courier_rate_options
                couriers = fetch_providers("COURIER", active_only=True) or []
            except Exception:
                couriers = []
                fetch_courier_rate_options = lambda *_a, **_k: []
            provider_ids = [""] + [str(c.get("id") or "") for c in couriers]

            def fmt_courier(pid):
                if not pid:
                    return "— Select Courier Provider —"
                c = next((x for x in couriers if str(x.get("id") or "") == str(pid)), {})
                return str(c.get("provider_name") or pid)

            cpick = st.selectbox("Courier provider", provider_ids, format_func=fmt_courier, key="qsb_courier_provider")
            cprov = next((x for x in couriers if str(x.get("id") or "") == str(cpick)), {})
            slabs = fetch_courier_rate_options(cpick, active_only=True) if cpick else []
            slab_ids = [""] + [str(s.get("id") or "") for s in slabs]
            slab_idx = 0
            if slabs:
                low = min(slabs, key=lambda s: float(s.get("charge_base") or 0))
                low_id = str(low.get("id") or "")
                slab_idx = slab_ids.index(low_id) if low_id in slab_ids else 0

            def fmt_slab(sid):
                if not sid:
                    return "Provider default / manual"
                s = next((x for x in slabs if str(x.get("id") or "") == str(sid)), {})
                code = str(s.get("parcel_size_code") or "")
                return f"{s.get('option_label') or ''}{' · ' + code if code else ''} — ₹{float(s.get('charge_base') or 0):,.2f}"

            spick = st.selectbox("Courier charge slab / parcel size", slab_ids, index=slab_idx, format_func=fmt_slab, key="qsb_courier_slab")
            slab = next((x for x in slabs if str(x.get("id") or "") == str(spick)), {})
            courier_meta = {
                "courier_provider_id": cpick,
                "courier_provider_name": str(cprov.get("provider_name") or ""),
                "courier_rate_option_id": spick,
                "courier_rate_option_label": str(slab.get("option_label") or ""),
                "courier_parcel_size": str(slab.get("parcel_size_code") or ""),
            }
            if slab:
                svc["default_price"] = float(slab.get("charge_base") or 0)
                svc["gst_percent"] = float(slab.get("gst_percent") or svc.get("gst_percent") or 18)
                st.caption("Lowest courier slab is auto-selected. Change dropdown if parcel is bigger.")

        c1, c2, c3 = st.columns([2.4, 1, 1])
        desc = c1.text_input("Description", value=str(svc.get("service_name") or group.title()), key="qsb_desc")
        amount = c2.number_input("Amount ₹", min_value=0.0, step=10.0, value=float(svc.get("default_price") or 0), key="qsb_amount")
        gst = c3.number_input("GST %", min_value=0.0, max_value=28.0, step=0.5, value=float(svc.get("gst_percent") or 18), key="qsb_gst")
        remarks = st.text_input("Remarks", key="qsb_remarks", placeholder="Reason / missed in billing / extra parcel...")

        if st.button("⚡ Create Service Bill", type="primary", use_container_width=True, key="qsb_create"):
            if not party.get("id"):
                st.error("Select a valid party.")
                return
            if amount <= 0:
                st.error("Enter service amount > 0.")
                return
            order_res = _create_quick_service_order(
                party=party,
                service=svc,
                desc=desc,
                base_amount=float(amount),
                gst_percent=float(gst),
                lens_params={
                    **courier_meta,
                    "quick_service_remarks": remarks,
                },
            )
            if not order_res.get("ok"):
                st.error(order_res.get("error") or "Quick service order failed.")
                return
            oid = order_res["order_id"]
            try:
                if route == "C":
                    from modules.billing.challan_invoice_manager import create_challan
                    ch_no = create_challan(
                        party_id=str(party.get("id")),
                        order_ids=[oid],
                        total_amount=float(order_res["base"]),
                        total_tax=float(order_res["gst"]),
                        remarks=remarks or "Quick service billing",
                    )
                    st.success(f"✅ Service challan created: {ch_no} · Order {order_res['order_no']}")
                    st.session_state.bo_challan_no = ch_no
                else:
                    from modules.billing.challan_invoice_manager import create_invoice
                    inv_no = create_invoice(
                        challan_id=None,
                        party_id=str(party.get("id")),
                        order_ids=[oid],
                        total_amount=float(order_res["base"]),
                        total_tax=float(order_res["gst"]),
                        due_days=0,
                        remarks=remarks or "Quick service billing",
                        party_name=str(party.get("party_name") or ""),
                    )
                    if inv_no:
                        st.success(f"✅ Service invoice created: {inv_no} · Order {order_res['order_no']}")
                        st.session_state.bo_invoice_no = inv_no
                    else:
                        st.error("Invoice was blocked by billing rules. The service order was created; open Billing Summary to create challan.")
            except Exception as exc:
                st.error(f"Service billing failed after order creation: {exc}")


def _add_to_cart(product_id, product_name, brand, main_group,
                 sph, cyl, axis, add_power, eye_side,
                 unit_price, gst_pct, party_type,
                 qty=None, box_price=None, box_size=1, box_qty=0,
                 loose_pcs=0, final_pcs=None, is_box_mode=False,
                 batch_no=None, stock_id=None, l_stock_id=None,
                 is_rl_split=False, lens_params=None):
    """
    Add one FIFO batch line to cart.
    When FIFO splits across batches, this is called once per batch.
    No merging — each batch is a separate cart line (separate order line).
    """
    # Normalize eye_side so cart and DB always receive R / L / B / SERVICE
    eye_side = normalize_eye_side(eye_side, service_aware=False)
    final_pcs = final_pcs if final_pcs is not None else (qty or 1)
    cart = st.session_state.bo_cart

    # Build display label
    pp = []
    if sph       is not None: pp.append(f"SPH {float(sph):+.2f}")
    if cyl       is not None: pp.append(f"CYL {float(cyl):+.2f}")
    if axis:                  pp.append(f"AX {int(axis)}")
    if add_power is not None: pp.append(f"ADD {float(add_power):+.2f}")
    power_str = " ".join(pp)

    is_pair   = is_rl_split and eye_side == "B"
    eye_label = ("Pair" if is_pair
                 else eye_side if eye_side else "")

    # Delegate GST calc to governor — handles inclusive/exclusive correctly
    from modules.core.price_qty_governor import compute_line_gst, normalize_box_total
    _calc_qty = max(1, int(final_pcs or qty or 1))
    _up       = float(unit_price or 0)
    _box_p    = float(box_price  or 0)
    _bsz      = max(1, int(box_size or 1))

    # Line total — use normalize_box_total for box products (no rounding error)
    if is_box_mode and _box_p > 0:
        _bq        = int(box_qty or 0)
        _lp        = int(loose_pcs or 0)
        _total_pcs = (_calc_qty * _bsz) if "contact" in str(main_group).lower() else _calc_qty
        line_total = normalize_box_total(_box_p, _bq * _bsz + _lp, {"box_size": _bsz})
    else:
        line_total = round(_up * _calc_qty, 2)

    # GST via governor
    _gst_r   = compute_line_gst(_up, _calc_qty if not is_box_mode else _bq * _bsz + int(loose_pcs or 0),
                                 float(gst_pct or 12), party_type)
    base     = _gst_r["gst_base"] / max(1, _calc_qty)   # per-pcs base
    gst_amt  = _gst_r["gst_amount"]

    _new_item = {
        "id":           str(uuid.uuid4()),
        "product_id":   product_id,
        "product_name": product_name,
        "brand":        brand,
        "main_group":   main_group,
        "sph":          sph,
        "cyl":          cyl,
        "axis":         axis,
        "add_power":    add_power,
        "eye_side":     eye_side,
        "eye_label":    eye_label,
        "power_str":    power_str,
        "is_pair":      is_pair,
        # Batch tracking
        "batch_no":     batch_no,
        "stock_id":     stock_id,
        "l_stock_id":   l_stock_id,
        "is_rl_split":  is_rl_split,
        # Pricing
        "unit_price":   unit_price,
        "box_price":    box_price,
        "box_size":     box_size,
        "box_qty":      box_qty,
        "loose_pcs":    loose_pcs,
        "is_box_mode":  is_box_mode,
        "lens_params":  lens_params or {},
        "base_price":   base,
        "gst_pct":      gst_pct,
        "gst_amt":      gst_amt,
        # Qty (always in pcs internally)
        "qty":          final_pcs,
        "final_pcs":    final_pcs,
        "line_total":   line_total,
        "billing_qty":  final_pcs,
        # Discount (stamped below)
        "discount_percent": 0.0,
        "discount_amount":  0.0,
    }

    # Stamp discount immediately at add-to-cart for correct UI pricing
    try:
        from modules.pricing.discount_engine import apply_discounts
        _bo_pid  = str(st.session_state.get("bo_party_id") or "")
        _bo_otype = str(st.session_state.get("bo_party_type") or "WHOLESALE")
        apply_discounts([_new_item], party_id=_bo_pid, order_type=_bo_otype)
        _disc = float(_new_item.get("discount_amount") or 0)
        if _disc > 0:
            _net = round(float(_new_item.get("line_total") or 0) - _disc, 2)
            _new_item["billing_total"] = _net
            # Recalc GST on net
            _gst_r2 = compute_line_gst(
                _new_item["unit_price"],
                int(final_pcs or 1),
                float(gst_pct or 12),
                _bo_otype
            )
            _new_item["gst_amt"] = _gst_r2["gst_amount"]
    except Exception:
        pass

    cart.append(_new_item)


def _line_product_with_spec(line: dict) -> str:
    """Product display name including ophthalmic index/coating/treatment."""
    pname = str(line.get("product_name") or "")
    lp = line.get("lens_params") or {}
    if not isinstance(lp, dict):
        lp = {}
    spec_src = lp or line.get("oph_spec") or {}
    if not isinstance(spec_src, dict):
        spec_src = {}
    suffix = str(spec_src.get("display_suffix") or "").strip()
    if suffix and suffix.lower() not in pname.lower():
        return f"{pname} {suffix}".strip()
    idx = spec_src.get("lens_index") or spec_src.get("index") or spec_src.get("index_value") or ""
    coating = spec_src.get("coating") or spec_src.get("coating_type") or ""
    treatment = spec_src.get("treatment") or ""
    spec = []
    if idx:
        spec.append(f"Index {idx}")
    if coating:
        spec.append(str(coating))
    if treatment and str(treatment).strip().lower() != "clear":
        spec.append(str(treatment))
    spec_txt = " | ".join(spec)
    if spec_txt and spec_txt.lower() not in pname.lower():
        return f"{pname} | {spec_txt}"
    return pname


def _render_cart():
    cart = st.session_state.bo_cart
    if not cart:
        st.info("Cart is empty. Add products above.")
        return

    # Normalize old cart items that may be missing new keys
    for item in cart:
        if "final_pcs" not in item:
            item["final_pcs"]   = item.get("qty", 1)
        if "box_qty" not in item:
            item["box_qty"]     = 0
        if "loose_pcs" not in item:
            item["loose_pcs"]   = item.get("final_pcs", 1)
        if "is_box_mode" not in item:
            item["is_box_mode"] = False
        if "box_price" not in item:
            item["box_price"]   = None
        if "box_size" not in item:
            item["box_size"]    = 1

    st.markdown(f"#### 🛒 Cart ({len(cart)} items)")

    grand_base  = 0.0
    grand_gst   = 0.0
    grand_total = 0.0

    for item in cart:
        with st.container():
            c1, c2, c3, c4, c5 = st.columns([4, 1, 1, 1, 1])
            pname = _line_product_with_spec(item)
            power = item["power_str"]
            eye   = item["eye_label"]
            label = f"**{pname}**"
            if power: label += f"  `{power}`"
            if eye:   label += f"  👁 {eye}"
            # Qty display
            _fpcs       = item.get("final_pcs") or item.get("qty", 1)
            _is_cl_item = "contact" in item.get("main_group","").lower()
            _bs_item    = max(1, int(item.get("box_size") or 1))
            _bq_item    = int(item.get("box_qty") or 0)
            _lp_item    = int(item.get("loose_pcs") or 0)
            _up_item    = float(item.get("unit_price") or 0)
            _bp_item    = float(item.get("box_price") or 0)

            # DB stores pcs — use reverse_qty for display
            from modules.core.price_qty_governor import reverse_qty as _rq_display, pcs_to_display
            _prod_dict_item = {
                "unit":       "BOX" if item.get("is_box_mode") else "PCS",
                "box_size":   _bs_item,
                "allow_loose": (_lp_item > 0),
            }
            _rev = _rq_display(_fpcs, _prod_dict_item)
            qty_label   = f"**{_rev['display']}**"
            if _bp_item > 0 and _bs_item > 1:
                price_label = f"₹{_bp_item:,.2f}/box · ₹{_up_item:.2f}/pcs"
            elif _up_item > 0:
                price_label = f"₹{_up_item:,.2f}/pcs"
            else:
                price_label = "—"

            # Show batch no on each line
            batch_badge = ""
            if item.get("batch_no"):
                exp = (item.get("expiry_date") or "")[:7]
                batch_badge = f"  `{item['batch_no']}`"
                if exp: batch_badge += f" exp:{exp}"
                if item.get("is_rl_split"): batch_badge += " R+L"
            c1.markdown(label + batch_badge)
            c2.markdown(qty_label)
            c3.markdown(price_label)

            # Inline qty edit
            _edit_help = "Edit qty (boxes)" if _is_cl_item else "Edit qty (pcs)"
            new_pcs = c4.number_input(
                "", min_value=1, value=int(item.get("final_pcs") or item.get("qty", 1)),
                key=f"cart_qty_{item['id']}",
                label_visibility="collapsed",
                help=_edit_help
            )
            if new_pcs != item["final_pcs"]:
                stk = _get_stock(item["product_id"],
                                  item.get("sph"), item.get("cyl"),
                                  item.get("axis"), item.get("add_power"),
                                  item.get("eye_side","B"))
                other_pcs = sum(
                    (it.get("final_pcs") or it.get("qty", 0)) for it in cart
                    if it["id"] != item["id"]
                    and it["product_id"] == item["product_id"]
                    and it.get("sph") == item.get("sph")
                )
                if other_pcs + new_pcs > stk["available"]:
                    st.error(f"Only {stk['available']} pcs available.")
                else:
                    _pt   = st.session_state.get("bo_party_type","RETAIL")
                    _up   = float(item.get("unit_price", 0))
                    _gst  = float(item.get("gst_pct", 12))
                    from modules.core.price_qty_governor import compute_line_gst as _clg_edit
                    _gst_r = _clg_edit(_up, new_pcs, _gst, _pt)
                    item["qty"]        = new_pcs
                    item["final_pcs"]  = new_pcs
                    item["line_total"] = _gst_r["grand_total"]
                    item["gst_amt"]    = _gst_r["gst_amount"]
                    item["base_price"] = round(_gst_r["gst_base"] / max(1, new_pcs), 2)
                    item["is_box_mode"]= item.get("is_box_mode", False)
                    if item.get("is_box_mode"):
                        bs = item.get("box_size", 1)
                        item["box_qty"]   = new_pcs // bs
                        item["loose_pcs"] = new_pcs %  bs
                    st.rerun()

            if c5.button("🗑️", key=f"cart_del_{item['id']}",
                         use_container_width=True):
                st.session_state.bo_cart = [
                    i for i in cart if i["id"] != item["id"]
                ]
                if not st.session_state.bo_cart:
                    bulk_hard_reset(keep_mode=True)
                st.rerun()

            # line_total is the authoritative total per line
            # For RETAIL: line_total = MRP * qty (GST inclusive)
            # base_price is per-pcs base, gst_amt is total GST for the line
            _qty = item.get("final_pcs") or item.get("qty", 1)
            _lt  = float(item.get("line_total") or 0)
            _ga  = float(item.get("gst_amt") or 0)
            grand_total += _lt
            grand_gst   += _ga
            # base = line_total - gst for retail (inclusive), base_price*qty for wholesale
            if st.session_state.get("bo_party_type","RETAIL") == "RETAIL":
                grand_base += _lt - _ga
            else:
                grand_base += float(item.get("base_price", item.get("unit_price",0))) * _qty

    # For RETAIL: line_total is MRP (GST inclusive), so grand_total = sum(line_total)
    # For WHOLESALE: line_total is base + GST, so grand_total = grand_base + grand_gst
    party_type_check = st.session_state.get("bo_party_type","RETAIL")
    if party_type_check == "RETAIL":
        # GST is inclusive — back-calculate
        _display_total   = grand_total   # already correct (sum of MRP * qty)
        _display_taxable = grand_base    # base already back-calculated in _add_to_cart
        _display_gst     = _display_total - _display_taxable
    else:
        # GST exclusive — add on top
        _display_taxable = grand_base
        _display_gst     = grand_gst
        _display_total   = grand_base + grand_gst

    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Taxable",    f"₹{_display_taxable:,.2f}")
    sc2.metric("GST",        f"₹{_display_gst:,.2f}")
    sc3.metric("Grand Total",f"₹{_display_total:,.2f}")

    # ── Optional advance at order stage ──────────────────────────────────
    _collect_adv = st.checkbox(
        "💳 Collect advance now",
        key="bo_collect_advance",
        value=st.session_state.get("bo_collect_advance", False),
        help="Record advance payment at order stage. Balance collected at invoice.",
    )
    if _collect_adv:
        _adv_modes = ["Cash","UPI","Card","Cheque","NEFT/RTGS"]
        _ac1, _ac2, _ac3 = st.columns(3)
        _adv_mode   = _ac1.selectbox("Mode", _adv_modes, key="bo_advance_mode")
        _adv_amount = _ac2.number_input(
            "Advance ₹", min_value=0.0, max_value=float(_display_total),
            value=0.0, step=0.50, key="bo_advance_amount",
        )
        _adv_ref = _ac3.text_input("Reference / UPI ID", key="bo_advance_ref",
                                    placeholder="Optional")
        if _adv_amount > 0:
            st.caption(
                f"Balance after advance: "
                f"**₹{max(_display_total - _adv_amount, 0):,.2f}**"
            )

    _enter_to_click()
    if st.button("🛍️ Place Order  [Enter]", type="primary",
                 use_container_width=True, key="bo_place_order"):
        _place_order()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 3 — PLACE ORDER
# ══════════════════════════════════════════════════════════════════════════════

def _place_order():
    cart             = st.session_state.bo_cart
    party_id         = st.session_state.bo_party_id
    party_name       = st.session_state.bo_party_name
    party_type       = st.session_state.bo_party_type
    billing_category = st.session_state.get("bo_billing_category", "ON_COMPLETION")
    credit_limit     = float(st.session_state.get("bo_credit_limit") or 0)

    if not cart:
        st.error("Cart is empty.")
        return

    # Schema columns ensured by modules/db/migrations/0001_billing_columns.sql
    # NOTE: FULL_ADVANCE parties — order placement is allowed freely.
    # The confirm gate fires in backoffice (order_status_live.check_confirm_gate).
    # No gate here — operator can punch, review, edit before payment.

    order_id = str(uuid.uuid4())
    order_no = _next_order_no(party_type)

    # ── Deduplicate cart (must happen before discount + INSERT) ───────────
    _seen_bulk = set()
    _deduped_bulk = []
    for _bi in cart:
        _bk = (
            str(_bi.get("product_id","") or ""),
            str(_bi.get("eye_side","") or "").upper()[:1],
            str(_bi.get("sph","") or ""),
            str(_bi.get("cyl","") or ""),
            str(_bi.get("add_power","") or ""),
            str(_bi.get("axis","") or ""),
        )
        if _bk not in _seen_bulk:
            _seen_bulk.add(_bk)
            _deduped_bulk.append(_bi)
    cart = _deduped_bulk

    # ── Apply discount rules BEFORE orders INSERT ─────────────────────────
    # Earlier this ran AFTER the orders INSERT, so total_value = sum(line_total)
    # which is gross. Moving it up lets us roll up the net header total and
    # store it correctly in orders.total_value. Each cart item gets
    # discount_amount stamped here; the loop below writes net to total_price
    # and billing_total.
    try:
        _bo_party_id = str(order_data.get("party_id") or
                          st.session_state.get("bo_party_id") or "")
        _bo_otype    = str(order_data.get("order_type", "wholesale"))
        # Normalise bulk item dicts to have billing_qty for the engine
        for _bi in cart:
            _bi.setdefault("billing_qty", _bi.get("qty", 1))
        apply_discounts(cart, party_id=_bo_party_id, order_type=_bo_otype)
    except Exception as _bde:
        pass  # zero-risk fallback

    # ── Compute net header total_value ────────────────────────────────────
    # For pair items the line_total already covers both eyes (we don't
    # duplicate it when splitting into R+L below), so summing once is correct.
    _bulk_net_total = 0.0
    for item in cart:
        _gross = float(item.get("line_total") or 0)
        _disc  = float(item.get("discount_amount") or 0)
        _net   = round(_gross - _disc, 2)
        if _net < 0:
            _net = 0.0
        item["billing_total"] = _net    # cache net on the item for the INSERT loop
        _bulk_net_total += _net

    # Create order header with NET total_value
    ok = _rw("""
        INSERT INTO orders
          (id, order_no, order_type, order_source,
           party_id, party_name,
           status, total_items, total_value,
           created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
        ON CONFLICT (id) DO NOTHING
    """, (
        order_id, order_no, party_type, "BULK",
        party_id, party_name,
        "CONFIRMED",   # COUNTER_SALE: stock verified at cart, skip backoffice confirm
        len(cart),
        round(_bulk_net_total, 2),
    ))
    if not ok:
        st.error("Failed to create order.")
        return

    # Create order lines
    # COUNTER_SALE lines: stock was verified + FIFO-allocated at cart build time.
    # allocated_qty = quantity (fully allocated), lens_params marks route as STOCK
    # so is_line_billing_ready() and _is_order_billing_ready() pass without
    # needing a separate allocation step.
    import json as _json
    _cs_base_lens = {
        "manufacturing_route": "STOCK",
        "order_source":        "BULK",
    }

    for item in cart:
        _item_lens = dict(_cs_base_lens)
        if isinstance(item.get("lens_params"), dict):
            _item_lens.update({k: v for k, v in item["lens_params"].items()
                               if v not in (None, "")})
        _cs_lens = _json.dumps(_item_lens)

        # Both total_price and billing_total now hold NET (post-discount).
        # billing_total was pre-computed in the cart loop above as
        # round(line_total - discount_amount, 2). Earlier total_price was
        # written as item["line_total"] (gross), creating an inconsistency
        # where reports reading total_price saw gross while reports reading
        # billing_total saw net. Now both are net.
        _net_for_insert = float(item.get("billing_total") or
                                round(float(item["line_total"]) -
                                      float(item.get("discount_amount", 0)), 2))
        if _net_for_insert < 0:
            _net_for_insert = 0.0

        # For a pair (B eye split) — create TWO lines: R + L
        if item["is_pair"]:
            for eye in ("R", "L"):
                _rw("""
                    INSERT INTO order_lines
                      (id, order_id, product_id, eye_side,
                       sph, cyl, axis, add_power,
                       quantity, unit_price, total_price,
                       gst_percent, gst_amount,
                       discount_percent, discount_amount,
                       billing_total, discount_rule, applied_rule_ids,
                       status, lens_params, boxing_params,
                       allocated_qty, billed_qty)
                    VALUES (%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,
                            'PENDING',%s,'{}', %s, 0)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    str(uuid.uuid4()), order_id, item["product_id"],
                    eye,
                    item.get("sph"), item.get("cyl"),
                    item.get("axis"), item.get("add_power"),
                    item["qty"],
                    item["unit_price"],
                    _net_for_insert,                             # total_price = NET
                    item["gst_pct"],
                    item["gst_amt"],
                    float(item.get("discount_percent", 0)),
                    float(item.get("discount_amount", 0)),
                    _net_for_insert,                             # billing_total = NET
                    str(item.get("discount_rule") or ""),
                    str(item.get("applied_rule_ids") or ""),
                    _cs_lens,
                    int(item.get("allocated_qty", item["qty"]) or 0),
                ))
        else:
            _rw("""
                INSERT INTO order_lines
                  (id, order_id, product_id, eye_side,
                   sph, cyl, axis, add_power,
                   quantity, unit_price, total_price,
                   gst_percent, gst_amount,
                   discount_percent, discount_amount,
                   billing_total, discount_rule, applied_rule_ids,
                   status, lens_params, boxing_params,
                   allocated_qty, billed_qty)
                VALUES (%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,
                        'PENDING',%s,'{}', %s, 0)
                ON CONFLICT (id) DO NOTHING
            """, (
                str(uuid.uuid4()), order_id, item["product_id"],
                item.get("eye_side"),
                item.get("sph"), item.get("cyl"),
                item.get("axis"), item.get("add_power"),
                item["qty"],
                item["unit_price"],         # per-pcs billed price
                _net_for_insert,            # total_price = NET (was gross)
                item["gst_pct"],
                item["gst_amt"],
                float(item.get("discount_percent", 0)),
                float(item.get("discount_amount", 0)),
                _net_for_insert,            # billing_total = NET
                str(item.get("discount_rule") or ""),
                str(item.get("applied_rule_ids") or ""),
                _cs_lens,                   # manufacturing_route = STOCK
                int(item.get("allocated_qty", item["qty"]) or 0),  # service production stays pending
            ))

    st.session_state.bo_order_id  = order_id
    st.session_state.bo_order_no  = order_no
    try:
        from modules.backoffice.backoffice_helpers import ensure_order_production_refs
        ensure_order_production_refs(order_no=order_no, order_id=order_id)
    except Exception as _prod_ref_bulk_err:
        import logging as _prod_ref_bulk_log
        _prod_ref_bulk_log.getLogger(__name__).warning(
            "Bulk order production_ref fill failed for %s: %s",
            order_no, _prod_ref_bulk_err,
        )

    # ── Record advance if collected at cart stage ─────────────────────────
    _adv_amt  = float(st.session_state.get("bo_advance_amount") or 0)
    _adv_mode = str(st.session_state.get("bo_advance_mode") or "Cash")
    _adv_ref  = str(st.session_state.get("bo_advance_ref") or "").strip()
    if st.session_state.get("bo_collect_advance") and _adv_amt > 0:
        try:
            try:
                from modules.db.order_number_registry import alloc_doc_number as _adn_ba
                _ba_pno = _adn_ba("PAYMENT")
            except Exception:
                import datetime as _badt
                _ba_pno = f"PAY/{_badt.date.today().strftime('%y%m')}/{uuid.uuid4().hex[:5].upper()}"
            _rw("""
                INSERT INTO payments
                  (id, payment_no, party_id, party_name,
                   order_id, advance_for_order_id,
                   payment_date, payment_mode, amount,
                   reference_no, payment_type, created_at)
                VALUES (%s,%s,%s::uuid,%s,
                        %s::uuid,%s::uuid,
                        NOW(),%s,%s,%s,'ADVANCE',NOW())
            """, (
                str(uuid.uuid4()), _ba_pno,
                party_id,
                st.session_state.get("bo_party_name","") or "",
                order_id, order_id,
                _adv_mode, _adv_amt,
                _adv_ref or None,
            ))
        except Exception as _ae:
            pass  # Advance failure must not block order save

    # ── Audit trail for duplicate orders ──────────────────────────────────
    _src_ono_bo = st.session_state.pop("_bo_source_order_no", "") or ""
    if st.session_state.get("_bo_duplicate_mode") and _src_ono_bo:
        try:
            _rw("""
                INSERT INTO order_status_history
                    (order_id, from_status, to_status, changed_by_name, remarks)
                VALUES (%s::uuid, 'PUNCHED_DUPLICATE', 'CONFIRMED', %s, %s)
            """, (
                order_id,
                st.session_state.get("user_name","Staff"),
                f"Duplicate of order {_src_ono_bo}",
            ))
        except Exception:
            pass

    st.session_state.bo_tab = 2
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 4 — CREATE CHALLAN
# ══════════════════════════════════════════════════════════════════════════════

def _render_challan_stage():
    order_id   = st.session_state.bo_order_id
    order_no   = st.session_state.bo_order_no
    party_id   = st.session_state.bo_party_id
    party_type = st.session_state.bo_party_type
    cart       = st.session_state.bo_cart

    st.success(f"✅ Order **{order_no}** placed successfully.")

    party_type  = st.session_state.bo_party_type
    # Correct totals depending on GST model
    # RETAIL: line_total = MRP (incl GST), base = line_total - gst_amt
    # WHOLESALE: line_total = base + GST, base = base_price * qty
    def _cart_totals(cart_items, ptype):
        from modules.core.price_qty_governor import compute_line_gst as _clg_ct
        base = gst = total = 0.0
        for it in cart_items:
            _up  = float(it.get("unit_price", 0))
            _qty = int(it.get("final_pcs") or it.get("qty", 1))
            _g   = float(it.get("gst_pct", 12))
            _r   = _clg_ct(_up, _qty, _g, ptype)
            total += _r["grand_total"]
            gst   += _r["gst_amount"]
            base  += _r["gst_base"]
        return round(base, 2), round(gst, 2), round(total, 2)

    total_base, total_gst, total_grand = _cart_totals(cart, party_type)

    # Show order summary
    st.markdown("#### Order Summary")
    for item in cart:
        eye_badge = f" · {item['eye_label']}" if item.get("eye_label") else ""
        pair_note = " *(R+L split on stock)*" if item.get("is_pair") else ""
        batch_info = f" [{item['batch_no']}]" if item.get("batch_no") else ""
        st.markdown(
            f"- **{_line_product_with_spec(item)}** {item.get('power_str','')}"
            f"{eye_badge} × {item.get('final_pcs') or item.get('qty',1)}"
            f" · ₹{item.get('line_total',0):,.2f}{pair_note}{batch_info}"
        )

    if party_type == "RETAIL":
        st.markdown(
            f"**Total: ₹{total_grand:,.2f}** "
            f"(Taxable: ₹{total_base:,.2f} + GST: ₹{total_gst:,.2f}) — GST inclusive"
        )
    else:
        st.markdown(
            f"**Base: ₹{total_base:,.2f} + GST: ₹{total_gst:,.2f} = "
            f"Total: ₹{total_grand:,.2f}** — GST exclusive"
        )

    remarks = st.text_input("Remarks (optional)", key="bo_challan_remarks",
                             placeholder="e.g. Counter sale")

    # ── Pre-challan validators ───────────────────────────────────────
    _can_challan = True

    # 1. Party must be set
    if not party_id:
        st.error("❌ No party selected.")
        _can_challan = False

    # 2. Credit limit check for ON_ACCOUNT
    _bc = st.session_state.get("bo_billing_category", "ON_COMPLETION")
    _cl = float(st.session_state.get("bo_credit_limit") or 0)
    if _can_challan and _bc == "ON_ACCOUNT" and _cl > 0:
        try:
            _out_r = _rq("""
                SELECT COALESCE(SUM(balance_due),0) AS outstanding
                FROM invoices
                WHERE party_id = %s::uuid
                  AND payment_status NOT IN ('PAID','CANCELLED')
            """, (party_id,))
            _outstanding = float(_out_r[0]["outstanding"] if _out_r else 0)
            if _outstanding + total_grand > _cl:
                st.error(
                    f"❌ Credit limit exceeded — outstanding ₹{_outstanding:,.2f} + "
                    f"this order ₹{total_grand:,.2f} > limit ₹{_cl:,.2f}. "
                    f"Cannot create challan."
                )
                _can_challan = False
        except Exception:
            pass  # credit check is best-effort

    # 3. FULL_ADVANCE: payment must be recorded before challan
    if _can_challan and _bc == "FULL_ADVANCE":
        _paid_r = _rq("""
            SELECT COALESCE(SUM(amount),0) AS paid
            FROM payments
            WHERE advance_for_order_id = %s::uuid
              AND COALESCE(is_deleted,FALSE)=FALSE
        """, (order_id,))
        _adv_paid = float(_paid_r[0]["paid"] if _paid_r else 0)
        if _adv_paid < total_grand - 0.01:
            st.error(
                f"❌ Full Advance required — ₹{_adv_paid:,.2f} received, "
                f"₹{total_grand - _adv_paid:,.2f} still pending. "
                f"Collect full payment before creating challan."
            )
            _can_challan = False

    _enter_to_click()
    if st.button("📋 Create Challan  [Enter]", type="primary",
                 use_container_width=True, key="bo_create_challan",
                 disabled=not _can_challan):
        try:
            from modules.billing.challan_invoice_manager import create_challan
            from modules.sql_adapter import run_query as _rq_bo
            # Get order line IDs
            line_rows = _rq("""
                SELECT id::text FROM order_lines
                WHERE order_id = %s::uuid
                  AND COALESCE(is_deleted,FALSE)=FALSE
            """, (order_id,))
            line_ids = [r["id"] for r in line_rows]

            # ── INHOUSE readiness gate ────────────────────────────────
            _bo_inhouse = _rq_bo("""
                SELECT ol.id::text AS line_id
                FROM order_lines ol
                WHERE ol.order_id = %s::uuid
                  AND UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'INHOUSE'
                  AND NOT COALESCE(ol.is_deleted, FALSE)
            """, (order_id,))
            _bo_block = []
            for _bo_il in (_bo_inhouse or []):
                try:
                    _bo_vbr = _rq_bo(
                        "SELECT * FROM public.validate_billing_readiness(%s::uuid)",
                        (_bo_il["line_id"],)
                    )
                    if _bo_vbr and not _bo_vbr[0].get("is_ready"):
                        _bo_block.append(_bo_vbr[0].get("block_reason") or "Not ready")
                except Exception as _bo_vbr_e:
                    # DB function absent (older deployment) — use Python fallback gate
                    import logging as _bo_log
                    _bo_log.getLogger(__name__).warning(
                        f"[bulk_order] validate_billing_readiness missing; using Python fallback: {_bo_vbr_e}"
                    )
                    try:
                        from modules.billing.challan_invoice_manager import is_line_billing_ready
                        _ok_fb, _reason_fb = is_line_billing_ready(_bo_il["line_id"])
                        if not _ok_fb:
                            _bo_block.append(_reason_fb)
                    except Exception as _fb_e:
                        _bo_block.append(f"Readiness check failed: {_fb_e}")
            if _bo_block:
                for _bm in _bo_block:
                    st.error(f"❌ {_bm}")
                st.stop()
            # ─────────────────────────────────────────────────────────

            challan_no = create_challan(
                party_id     = party_id,
                order_ids    = [order_id],
                total_amount = round(total_base, 2),
                total_tax    = round(total_gst, 2),
                remarks      = remarks,
                line_ids     = line_ids,
            )
            if challan_no:
                st.session_state.bo_challan_no = challan_no
                st.session_state.bo_tab        = 3
                st.rerun()
            else:
                st.error("Challan creation failed.")
        except Exception as e:
            st.error(f"Challan error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 5 — INVOICE + PAYMENT
# ══════════════════════════════════════════════════════════════════════════════

def _render_invoice_stage():
    challan_no  = st.session_state.bo_challan_no
    party_id    = st.session_state.bo_party_id
    party_name  = st.session_state.bo_party_name
    party_type  = st.session_state.bo_party_type
    order_id    = st.session_state.bo_order_id
    cart        = st.session_state.bo_cart
    invoice_id  = st.session_state.bo_invoice_id
    invoice_no  = st.session_state.bo_invoice_no

    st.success(f"✅ Challan **{challan_no}** created.")

    party_type_inv = st.session_state.bo_party_type
    def _cart_totals_inv(cart_items, ptype):
        base = gst = total = 0.0
        for it in cart_items:
            _lt  = float(it.get("line_total", 0))
            _ga  = float(it.get("gst_amt", 0))
            _bp  = float(it.get("base_price", it.get("unit_price", 0)))
            _qty = int(it.get("final_pcs") or it.get("qty", 1))
            total += _lt
            gst   += _ga
            base  += (_lt - _ga) if ptype == "RETAIL" else (_bp * _qty)
        return round(base, 2), round(gst, 2), round(total, 2)

    total_base, total_gst, total_grand = _cart_totals_inv(cart, party_type_inv)

    # Get billing category — drives invoice rules
    billing_cat = st.session_state.get("bo_billing_category") or (
        "ADVANCE_BALANCE" if party_type == "RETAIL" else "ON_COMPLETION"
    )

    _bc_labels = {
        "FULL_ADVANCE":    "💵 Full Advance — full payment required BEFORE order is placed",
        "ADVANCE_BALANCE": "🛍️ Advance + Balance — advance at booking, balance before invoice",
        "PRE_PAYMENT":     "💳 Pre-Payment — full payment required before invoice",
        "ON_COMPLETION":   "📦 On Completion — invoice on dispatch, pay within credit days",
        "ON_ACCOUNT":      "📒 On Account — running ledger, periodic settlement",
    }
    st.info(f"**Billing policy:** {_bc_labels.get(billing_cat, billing_cat)}")

    # Payment required before invoice for ADVANCE_BALANCE and PRE_PAYMENT
    requires_payment_first = billing_cat in ("ADVANCE_BALANCE", "PRE_PAYMENT")

    # ── Payment section ───────────────────────────────────────────────
    st.markdown("#### 💰 Payment")

    if not invoice_id:
        _render_payment_collection(
            order_id    = order_id,
            party_id    = party_id,
            party_name  = party_name,
            party_type  = party_type,
            total_grand = total_grand,
        )

    # ── Invoice creation ──────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🧾 Invoice")

    if invoice_id:
        st.success(f"✅ Invoice **{invoice_no}** created.")
        _render_print_actions(invoice_no, challan_no)
        return

    # Check payment status
    paid_r = _rq("""
        SELECT COALESCE(SUM(amount),0) AS paid
        FROM payments
        WHERE advance_for_order_id = %s::uuid
          AND COALESCE(is_deleted,FALSE)=FALSE
    """, (order_id,))
    paid = float(paid_r[0]["paid"] if paid_r else 0)

    try:
        from modules.core.business_rules import billing_blocks_invoice
        credit_limit = float(st.session_state.get("bo_credit_limit") or 0)
        # Outstanding balance for ON_ACCOUNT (simplified — sum open invoices)
        outstanding = 0.0
        if billing_cat == "ON_ACCOUNT" and credit_limit > 0:
            out_r = _rq("""
                SELECT COALESCE(SUM(balance_due),0) AS outstanding
                FROM invoices
                WHERE party_id = %s::uuid
                  AND payment_status NOT IN ('PAID','CANCELLED')
            """, (party_id,))
            outstanding = float(out_r[0]["outstanding"] if out_r else 0)

        _blocked, _reason = billing_blocks_invoice(
            billing_cat, paid, total_grand, outstanding, credit_limit
        )
        can_invoice = not _blocked
        if _blocked:
            st.warning(f"⚠️ {_reason}")
        elif billing_cat in ("ON_COMPLETION","ON_ACCOUNT"):
            if paid > 0:
                st.success(f"✅ ₹{paid:,.2f} received. Invoice on credit for balance.")
            else:
                st.info("📋 Credit billing — invoice will proceed without advance payment.")
    except ImportError:
        can_invoice = True
        if requires_payment_first and paid < total_grand - 0.01:
            can_invoice = False
            st.warning(f"⚠️ Payment incomplete — collect ₹{total_grand - paid:,.2f} more.")

    inv_remarks = st.text_input("Invoice remarks", key="bo_inv_remarks",
                                 placeholder="Optional")

    _enter_to_click()
    if st.button("🧾 Generate Invoice  [Enter]", type="primary",
                 use_container_width=True, key="bo_gen_invoice",
                 disabled=not can_invoice):
        try:
            from modules.billing.challan_invoice_manager import create_invoice
            # Get challan id
            chl = _rq("""
                SELECT id::text FROM challans WHERE challan_no=%s LIMIT 1
            """, (challan_no,))
            challan_id = chl[0]["id"] if chl else None

            inv_no = create_invoice(
                challan_id   = challan_id,
                party_id     = party_id,
                order_ids    = [order_id],
                total_amount = round(total_base, 2),
                total_tax    = round(total_gst, 2),
                due_days     = 0 if party_type == "RETAIL" else 30,
                remarks      = inv_remarks,
            )
            if inv_no:
                # Get invoice id
                inv_row = _rq("""
                    SELECT id::text FROM invoices WHERE invoice_no=%s LIMIT 1
                """, (inv_no,))
                st.session_state.bo_invoice_id  = inv_row[0]["id"] if inv_row else None
                st.session_state.bo_invoice_no  = inv_no
                # Update order status to BILLED
                _rw("""
                    UPDATE orders SET status='BILLED', updated_at=NOW()
                    WHERE id=%s::uuid
                """, (order_id,))
                # ── Lock job stages — prevent rollback after invoice ──
                try:
                    from modules.sql_adapter import run_query as _rq_bjl, run_scalar as _rs_bjl
                    _jm_bl = _rq_bjl("""
                        SELECT jm.id::text
                        FROM job_master jm
                        JOIN order_lines ol ON ol.id = jm.order_line_id
                        WHERE ol.order_id = %s::uuid
                    """, (order_id,))
                    for _jbl in (_jm_bl or []):
                        _rs_bjl(
                            "SELECT public.set_job_billed_lock(%s::uuid)",
                            (_jbl["id"],)
                        )
                except Exception: pass
                # ── WhatsApp — Invoice ────────────────────────────────
                try:
                    from modules.wa_hub import wa_document_attachment, wa_panel, wa_invoice_made
                    from modules.settings.shop_master import get_unit_info
                    _sh_i = get_unit_info("wholesale" if party_type == "WHOLESALE" else "retail")
                    _mob_i = _rq("SELECT COALESCE(mobile,'') AS m FROM parties WHERE id=%s::uuid LIMIT 1", (party_id,))
                    wa_panel(
                        mobile = _mob_i[0]["m"] if _mob_i else "",
                        msg    = wa_invoice_made(
                            party      = party_name,
                            invoice_no = inv_no,
                            grand_total= float(total_incl),
                            balance    = max(float(total_incl) - float(paid or 0), 0),
                            shop_name  = _sh_i.get("shop_name","DV Optical"),
                            phone      = _sh_i.get("shop_phone",""),
                            upi_id     = _sh_i.get("shop_upi_id",""),
                        ),
                        key     = f"wa_bo_inv_{inv_no}",
                        title   = "📲 WhatsApp — Invoice Generated",
                        expanded= True,
                        party_name = party_name,
                        attachments=[
                            wa_document_attachment("invoice", inv_no)
                        ],
                    )
                except Exception:
                    pass
                st.rerun()
            else:
                st.error("Invoice creation failed.")
        except Exception as e:
            st.error(f"Invoice error: {e}")


def _render_payment_collection(order_id, party_id, party_name,
                                party_type, total_grand):
    """Inline payment collection."""
    # Check existing payments
    paid_r = _rq("""
        SELECT COALESCE(SUM(amount),0) AS paid
        FROM payments
        WHERE advance_for_order_id = %s::uuid
          AND COALESCE(is_deleted,FALSE)=FALSE
    """, (order_id,))
    paid    = float(paid_r[0]["paid"] if paid_r else 0)
    balance = max(total_grand - paid, 0)

    if paid > 0:
        st.markdown(
            f"<div style='background:#0d2818;border:1px solid #22c55e;"
            f"border-radius:6px;padding:6px 14px;margin-bottom:8px'>"
            f"✅ Received: <b style='color:#4ade80'>₹{paid:,.2f}</b>"
            f" · Balance: <b style='color:#f59e0b'>₹{balance:,.2f}</b>"
            f"</div>",
            unsafe_allow_html=True
        )
        if balance <= 0:
            return

    modes = ["Cash","UPI","Card","Credit","Cheque","NEFT/RTGS"]
    pc1, pc2, pc3 = st.columns(3)
    pmode  = pc1.selectbox("Mode", modes, key="bo_pay_mode")
    pamount= pc2.number_input("Amount ₹", min_value=0.0,
                               value=float(balance), step=0.50,
                               key="bo_pay_amount")
    pref   = pc3.text_input("Reference / UPI ID", key="bo_pay_ref",
                             placeholder="Optional")

    if st.button("💳 Record Payment", use_container_width=True,
                 key="bo_record_payment"):
        if pamount <= 0:
            st.error("Enter amount.")
            return
        try:
            from modules.db.order_number_registry import alloc_doc_number as _adn_bp
            _bp_pno = _adn_bp("PAYMENT")
        except Exception:
            import datetime as _bpdt
            _bp_pno = f"PAY/{_bpdt.date.today().strftime('%y%m')}/{uuid.uuid4().hex[:5].upper()}"
        ok = _rw("""
            INSERT INTO payments
              (id, payment_no, party_id, party_name,
               order_id, advance_for_order_id,
               payment_date, payment_mode, amount,
               reference_no, payment_type, created_at)
            VALUES (%s,%s,%s::uuid,%s,
                    %s::uuid,%s::uuid,
                    NOW(),%s,%s,%s,'PAYMENT',NOW())
        """, (
            str(uuid.uuid4()), _bp_pno,
            party_id,
            st.session_state.get("bo_party_name","") or pname or "",
            order_id, order_id,
            pmode, pamount,
            pref.strip() or None,
        ))
        if ok:
            st.success(f"✅ ₹{pamount:,.2f} recorded via {pmode}")
            st.rerun()


def _render_print_actions(invoice_no, challan_no):
    """Post-invoice top bar: Duplicate/New, real WhatsApp, print actions."""

    # ── Build post-save snapshot if not already done ──────────────────────
    if not st.session_state.get("_bo_post_save_data"):
        _tot = sum(float(i.get("line_total",0)) for i in st.session_state.get("bo_cart",[]))
        st.session_state["_bo_post_save_data"] = {
            "order_no":    st.session_state.get("bo_order_no",""),
            "invoice_no":  invoice_no,
            "challan_no":  challan_no,
            "party_name":  st.session_state.get("bo_party_name",""),
            "mobile":      _scalar(
                "SELECT COALESCE(mobile,'') FROM parties WHERE id=%s::uuid LIMIT 1",
                (st.session_state.get("bo_party_id",""),)
            ) or "",
            "total":       _tot,
            "order_type":  st.session_state.get("bo_mode","RETAIL"),
            # Duplicate snapshot
            "_party_id":         st.session_state.get("bo_party_id",""),
            "_party_name":       st.session_state.get("bo_party_name",""),
            "_party_type":       st.session_state.get("bo_party_type","RETAIL"),
            "_mode":             st.session_state.get("bo_mode","RETAIL"),
            "_billing_category": st.session_state.get("bo_billing_category","ADVANCE_BALANCE"),
            "_cart":             list(st.session_state.get("bo_cart",[])),
        }

    _psd = st.session_state.get("_bo_post_save_data") or {}

    # ── What next? bar ────────────────────────────────────────────────────
    st.markdown(
        "<div style='background:#0a1628;border:2px solid #22c55e;"
        "border-radius:8px;padding:4px 8px;margin-bottom:8px'>"
        "<div style='color:#4ade80;font-size:0.72rem;font-weight:700;"
        "text-align:center;margin-bottom:4px'>Order done ✓ What next?</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _na1, _na2 = st.columns(2)

    with _na1:
        if st.button("📄 Duplicate Order", key="bo_dup_top",
                     use_container_width=True,
                     help="Same party + cart — new order/invoice"):
            _src = _psd.get("order_no","")
            duplicate_current_bulk_order()
            if _src:
                st.session_state["_bo_source_order_no"] = _src
            st.session_state.pop("_bo_post_save_data", None)
            st.rerun()

    with _na2:
        if st.button("🔄 New Order", key="bo_new_top",
                     type="primary", use_container_width=True):
            bulk_hard_reset(keep_mode=True)
            st.rerun()

    st.markdown("---")

    # ── Real WhatsApp via post_save_actions ───────────────────────────────
    _party_name = _psd.get("party_name","")
    _mobile     = _psd.get("mobile","")
    _total      = float(_psd.get("total",0))
    _ono        = _psd.get("order_no","")
    _otype      = _psd.get("order_type","RETAIL")
    _lines      = _psd.get("_cart",[])

    if _mobile:
        try:
            from modules.post_save_actions import render_post_save_actions
            render_post_save_actions(
                order_no    = _ono,
                party_name  = _party_name,
                mobile      = _mobile,
                total       = _total,
                order_type  = _otype,
                advance     = 0.0,
                delivery_date="",
                on_account  = (_otype == "WHOLESALE"),
                lines       = _lines,
                status_label= "CONFIRMED",
            )
        except Exception as _wae:
            st.caption(f"WhatsApp: {_wae}")
    else:
        st.caption("📱 No mobile on file for this party — WhatsApp not available.")

    st.markdown("---")

    # ── Print actions ─────────────────────────────────────────────────────
    st.markdown("#### 🖨️ Print")
    col1, col2 = st.columns(2)

    with col1:
        _inv_print_key = f"_bo_print_inv_{invoice_no}"
        _inv_open = st.session_state.get(_inv_print_key, False)
        if st.button(
            "✕ Close Invoice" if _inv_open else "🖨️ Print Invoice",
            use_container_width=True, key="bo_print_invoice"
        ):
            st.session_state[_inv_print_key] = not _inv_open
            st.rerun()
        if st.session_state.get(_inv_print_key):
            try:
                from modules.billing.smart_print import render_smart_invoice
                render_smart_invoice(invoice_no)
            except Exception as _pe:
                st.info(f"Open print from Billing → Invoices → {invoice_no}")

    with col2:
        _ch_print_key = f"_bo_print_ch_{challan_no}"
        _ch_open = st.session_state.get(_ch_print_key, False)
        if st.button(
            "✕ Close Challan" if _ch_open else "📋 Print Challan",
            use_container_width=True, key="bo_print_challan"
        ):
            st.session_state[_ch_print_key] = not _ch_open
            st.rerun()
        if st.session_state.get(_ch_print_key):
            try:
                from modules.billing.smart_print import render_smart_challan
                render_smart_challan(challan_no)
            except Exception as _pe:
                st.info(f"Open print from Billing → Challans → {challan_no}")


# ══════════════════════════════════════════════════════════════════════════════
# STAGE INDICATOR
# ══════════════════════════════════════════════════════════════════════════════

def _render_stages():
    tab = st.session_state.get("bo_tab", 0)
    stages = [
        ("🏢", "Party"),
        ("🛒", "Cart"),
        ("📦", "Order"),
        ("📋", "Challan"),
        ("🧾", "Invoice"),
    ]
    cols = st.columns(len(stages))
    for i, (icon, label) in enumerate(stages):
        done   = i < tab
        active = i == tab
        bg     = "#0d2818" if done else "#0d1a2e" if active else "#0f172a"
        border = "#22c55e" if done else "#3b82f6" if active else "#1e293b"
        color  = "#4ade80" if done else "#60a5fa" if active else "#475569"
        check  = "✓ " if done else ""
        cols[i].markdown(
            f"<div style='background:{bg};border:1px solid {border};"
            f"border-radius:8px;padding:6px;text-align:center'>"
            f"<div style='font-size:1.2rem'>{icon}</div>"
            f"<div style='color:{color};font-size:0.65rem;font-weight:700'>"
            f"{check}{label}</div></div>",
            unsafe_allow_html=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_bulk_order():
    _init()

    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
        "<span style='background:#f59e0b;color:#1c1917;font-size:0.7rem;font-weight:800;"
        "padding:3px 10px;border-radius:20px;letter-spacing:.06em'>"
        "⚡ BULK ORDER</span>"
        "<span style='color:#94a3b8;font-size:0.72rem'>Fast counter billing · Stock checked · Full order tracking</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Mode selector ─────────────────────────────────────────────────
    mc1, mc2, _ = st.columns([2, 2, 3])
    with mc1:
        if st.button("🛍️ Retail Counter",
                     type="primary" if st.session_state.bo_mode == "RETAIL" else "secondary",
                     use_container_width=True, key="bo_mode_retail"):
            if st.session_state.bo_mode != "RETAIL":
                st.session_state.bo_mode = "RETAIL"
                _reset()
            st.rerun()
    with mc2:
        if st.button("🏭 Wholesale",
                     type="primary" if st.session_state.bo_mode == "WHOLESALE" else "secondary",
                     use_container_width=True, key="bo_mode_wholesale"):
            if st.session_state.bo_mode != "WHOLESALE":
                st.session_state.bo_mode = "WHOLESALE"
                _reset()
            st.rerun()

    mode = st.session_state.bo_mode

    # Mode badge
    if mode == "RETAIL":
        st.markdown(
            "<div style='background:#0d2818;border:1px solid #22c55e;"
            "border-radius:8px;padding:6px 14px;margin:8px 0'>"
            "🛍️ <b style='color:#4ade80'>Retail Counter</b>"
            "<span style='color:#6b7280;font-size:0.8rem;margin-left:10px'>"
            "Patients · Walk-ins · MRP (GST incl.) · Payment before invoice"
            "</span></div>",
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            "<div style='background:#0a1628;border:1px solid #3b82f6;"
            "border-radius:8px;padding:6px 14px;margin:8px 0'>"
            "🏭 <b style='color:#60a5fa'>Wholesale</b>"
            "<span style='color:#6b7280;font-size:0.8rem;margin-left:10px'>"
            "Dealers · Distributors · Selling price (ex-GST) · Credit allowed"
            "</span></div>",
            unsafe_allow_html=True
        )

    # Ensure party_type matches mode when at party stage
    if st.session_state.get("bo_tab", 0) == 0:
        st.session_state.bo_party_type = mode

    # Stage indicator + reset
    hc1, hc2 = st.columns([5, 1])
    with hc1:
        _render_stages()
    with hc2:
        st.write("")
        if st.button("🔄 New", use_container_width=True, key="bo_reset_top"):
            bulk_hard_reset(keep_mode=True)
            st.rerun()

    _render_quick_service_billing()

    tab = st.session_state.get("bo_tab", 0)

    if tab == 0:
        _render_party_stage_mode(mode)
    elif tab == 1:
        _render_cart_stage()
    elif tab == 2:
        _render_challan_stage()
    elif tab == 3:
        _render_invoice_stage()
