"""
Supplier Panel
==============
PO lifecycle using advance_supplier_stage() DB function.

SCHEMA (from DB backup):
  supplier_orders            : id(int), supplier_order_id(varchar), supplier_id,
                               supplier_name, customer_order_id(varchar→orders.order_no),
                               order_date, expected_delivery_date, status,
                               total_items, total_qty, total_value,
                               created_by, created_at, updated_at, last_receipt_date
  supplier_order_items       : id(int), supplier_order_id(int FK), item_no, product_id,
                               product_name, brand, eye_side, sph, cyl, axis, add_power,
                               ordered_qty, received_qty, pending_qty,
                               unit_price, total_price, customer_line_id, item_status
  supplier_order_status_history: id(int), supplier_order_id(int), status,
                               timestamp, notes, changed_by
  supplier_stage_master      : id(uuid), stage_code, sequence_order, is_active
  supplier_stage_transitions : id(uuid), from_stage, to_stage, allowed(bool)
  DB FUNCTION: advance_supplier_stage(p_supplier_order_id int, p_next_stage varchar, p_user int)
               → text  ('SUCCESS' or 'ERROR: ...')

GOVERNANCE:
  - All stage transitions go through advance_supplier_stage() — DB enforces rules
  - received_qty cannot exceed ordered_qty (enforced by max_value in UI AND pending_qty in DB)
  - Status is always DB-read on page load
"""

import streamlit as st
from typing import Dict, List, Optional

from .event_logger import log_event, EventType


def _q(sql, params):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _sync_supplier_orders_id_sequence() -> None:
    try:
        from modules.sql_adapter import run_write
        run_write("""
            SELECT setval(
                pg_get_serial_sequence('supplier_orders','id'),
                GREATEST((SELECT COALESCE(MAX(id), 0) FROM supplier_orders), 1),
                TRUE
            )
        """, {})
    except Exception:
        pass


def _fetch_supplier_stages():
    """Ordered stage list from supplier_stage_master."""
    return _q(
        "SELECT stage_code, sequence_order FROM supplier_stage_master "
        "WHERE is_active=TRUE ORDER BY sequence_order ASC", {}
    )


def _fetch_allowed_next(current_status):
    rows = _q(
        "SELECT to_stage FROM supplier_stage_transitions "
        "WHERE from_stage=%(s)s AND allowed=TRUE", {"s": current_status}
    )
    return [r["to_stage"] for r in rows]


def _fetch_pos(order_id):
    """order_id = orders.order_no (text) = customer_order_id on supplier_orders."""
    return _q("""
        SELECT so.id, so.supplier_order_id, so.supplier_name, so.status,
               so.order_date, so.expected_delivery_date,
               so.total_items, so.total_qty, so.total_value,
               so.created_at, so.last_receipt_date,
               COALESCE(SUM(soi.received_qty), 0) AS total_received,
               COALESCE(SUM(soi.ordered_qty),  0) AS total_ordered
        FROM supplier_orders so
        LEFT JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
        WHERE so.customer_order_id = %(ono)s
        GROUP BY so.id
        ORDER BY so.order_date DESC
    """, {"ono": order_id})


def _fetch_po_items(po_id_int):
    return _q("""
        SELECT id, item_no, product_name, brand, eye_side,
               sph, cyl, axis, add_power,
               ordered_qty, received_qty, pending_qty,
               unit_price, total_price, item_status
        FROM supplier_order_items
        WHERE supplier_order_id = %(p)s
        ORDER BY item_no
    """, {"p": po_id_int})


def _advance_po_stage(po_id_int, order_id, next_stage):
    """Calls advance_supplier_stage() DB function."""
    try:
        from modules.sql_adapter import run_scalar
        result = run_scalar(
            "SELECT public.advance_supplier_stage(%(p)s, %(s)s, 0)",
            {"p": po_id_int, "s": next_stage}
        )
        if result and str(result).startswith("ERROR"):
            st.error(f"Transition blocked: {result}")
            return False
        ev_map = {
            "SENT":         EventType.STAGE_ADVANCED,
            "ACKNOWLEDGED": EventType.STAGE_ADVANCED,
            "RECEIVED":     EventType.STAGE_ADVANCED,
            "PARTIAL":      EventType.STAGE_ADVANCED,
            "INSPECTION":   EventType.STAGE_ADVANCED,
            "COMPLETE":     EventType.STAGE_ADVANCED,
            "READY_TO_BILL":EventType.STAGE_ADVANCED,
            "CLOSED":       EventType.STAGE_ADVANCED,
        }
        log_event(ev_map.get(next_stage, EventType.STAGE_ADVANCED), order_id=order_id,
                  details={"po_id": po_id_int, "status": next_stage}, source="user")

        # ── After RECEIVED: auto-advance to INSPECTION ───────────────────
        if next_stage == "RECEIVED":
            try:
                _insp = run_scalar(
                    "SELECT public.advance_supplier_stage(%(p)s, 'INSPECTION', 0)",
                    {"p": po_id_int}
                )
                if _insp and not str(_insp).startswith("ERROR"):
                    log_event(EventType.STAGE_ADVANCED, order_id=order_id,
                              details={"po_id": po_id_int, "status": "INSPECTION",
                                       "auto": True}, source="system")
            except Exception:
                pass  # best-effort — user can manually advance if DB transition not set

        return True
    except Exception as e:
        st.error(f"PO stage error: {e}")
        return False


def _save_received_qtys(po_id_int, received_map):
    """
    Write received_qty per item AND sync order_lines.ready_qty.
    This is the critical step that unlocks the billing junction for
    VENDOR and EXTERNAL_LAB train orders.
    GOVERNANCE: received_qty capped at ordered_qty by UI max_value.
    """
    try:
        from modules.sql_adapter import run_write, run_query
        for item_id, rcv in received_map.items():
            run_write("""
                UPDATE supplier_order_items
                SET received_qty = %(rcv)s,
                    pending_qty  = GREATEST(0, ordered_qty - %(rcv)s),
                    item_status  = CASE WHEN %(rcv)s >= ordered_qty THEN 'RECEIVED'
                                        WHEN %(rcv)s > 0 THEN 'PARTIAL'
                                        ELSE item_status END
                WHERE id = %(id)s AND supplier_order_id = %(po)s
            """, {"rcv": rcv, "id": item_id, "po": po_id_int})

            # ── Sync order_lines.ready_qty ──────────────────────────────
            # customer_line_id on supplier_order_items references order_lines.id
            # ready_qty drives the billing junction check in backoffice_ui.py
            _link = run_query("""
                SELECT customer_line_id, received_qty
                FROM supplier_order_items
                WHERE id = %(id)s
            """, {"id": item_id})
            if _link and _link[0].get("customer_line_id"):
                _line_id = _link[0]["customer_line_id"]
                _rcv     = int(_link[0].get("received_qty") or 0)
                # Ensure ready_qty column exists (migration may not have run)
                try:
                    run_write(
                        "ALTER TABLE order_lines ADD COLUMN IF NOT EXISTS ready_qty INTEGER DEFAULT 0",
                        {}
                    )
                except Exception:
                    pass
                try:
                    run_write("""
                        UPDATE order_lines
                        SET ready_qty = GREATEST(COALESCE(ready_qty, 0), %(r)s)
                        WHERE id = %(lid)s::uuid
                    """, {"r": _rcv, "lid": str(_line_id)})
                except Exception:
                    pass  # non-fatal — billing readiness recalculated on next load
        # ── After all qtys saved: check if order is fully ready for billing ──
        # If every order_line linked to this PO now has ready_qty >= quantity,
        # auto-advance PO to READY_TO_BILL so billing gate opens.
        try:
            _pending_lines = run_query("""
                SELECT ol.id
                FROM supplier_order_items soi
                JOIN order_lines ol ON ol.id::text = soi.customer_line_id::text
                WHERE soi.supplier_order_id = %(po)s
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND COALESCE(ol.ready_qty, 0) < COALESCE(ol.quantity, 1)
            """, {"po": po_id_int})
            if not _pending_lines:
                # All lines ready — advance to READY_TO_BILL
                from modules.sql_adapter import run_scalar as _rsc2
                _rb = _rsc2(
                    "SELECT public.advance_supplier_stage(%(p)s, 'READY_TO_BILL', 0)",
                    {"p": po_id_int}
                )
                # Suppress errors — transition may already be at READY_TO_BILL
        except Exception:
            pass  # never block save
        try:
            from modules.backoffice.backoffice_helpers import load_orders_from_database
            load_orders_from_database.clear()
        except Exception:
            pass
        return True
    except Exception as e:
        st.error(f"Receipt save failed: {e}")
        return False


STAGE_ICONS  = {"DRAFT":"📝","SENT":"📤","ACKNOWLEDGED":"👍",
                "PARTIAL":"⚡","RECEIVED":"📬","CLOSED":"🔒"}
STAGE_COLORS = {"DRAFT":"#6b7280","SENT":"#3b82f6","ACKNOWLEDGED":"#8b5cf6",
                "PARTIAL":"#f59e0b","RECEIVED":"#10b981","CLOSED":"#374151"}


def _build_bonzer_autofill_url(order: dict, items: list) -> str:
    """
    Build the Bonzer autofill URL for a supplier order.
    Encodes order + Rx data as base64 in the URL hash so the
    Tampermonkey script can read it and fill the Bonzer form.
    """
    import json, base64

    # Pull patient name and mobile from order
    patient  = str(order.get("patient_name") or order.get("party_name") or "")
    mobile   = str(order.get("patient_mobile") or order.get("mobile") or "")
    order_no = str(order.get("order_no") or "")

    # Build R and L dicts from items
    right = next((it for it in items if str(it.get("eye_side","")).upper() in ("R","RIGHT")), None)
    left  = next((it for it in items if str(it.get("eye_side","")).upper() in ("L","LEFT")),  None)

    def _rx(it):
        if not it:
            return None
        return {
            "product": str(it.get("product_name","")).strip(),
            "qty":     int(it.get("ordered_qty") or 1),
            "sph":     _fmt_pwr(it.get("sph")),
            "cyl":     _fmt_pwr(it.get("cyl")),
            "axis":    str(int(float(it["axis"]))) if it.get("axis") else "",
            "add":     _fmt_pwr(it.get("add_power")),
        }

    def _fmt_pwr(v):
        if v is None:
            return ""
        try:
            f = float(v)
            return f"{f:+.2f}"
        except Exception:
            return str(v)

    payload = {
        "order_no":       order_no,
        "customer_name":  patient,
        "customer_mobile": mobile,
        "right":          _rx(right),
        "left":           _rx(left),
        "master_brand":   "",   # staff picks
        "notes":          f"ERP order {order_no}",
    }

    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    return (
        "https://www.bonzerlenses.com/orders/add"
        f"#erprx={encoded}"
    )


def _render_timeline(stages, current_status):
    display = [s["stage_code"] for s in stages if s["stage_code"] != "PARTIAL"]
    cur     = display.index(current_status) if current_status in display else 0
    if current_status == "PARTIAL" and "ACKNOWLEDGED" in display:
        cur = display.index("ACKNOWLEDGED")
    if not display:
        return
    cols = st.columns(len(display))
    for i, (col, code) in enumerate(zip(cols, display)):
        icon  = STAGE_ICONS.get(code, "•")
        color = STAGE_COLORS.get(code, "#6b7280")
        with col:
            if i < cur:
                st.markdown(f"<div style='text-align:center;color:#10b981'>{icon}</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:#10b981'>✓ {code}</div>",
                            unsafe_allow_html=True)
            elif i == cur:
                st.markdown(f"<div style='text-align:center;color:{color}'>{icon}</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:{color};"
                            f"font-weight:700;background:#f3f4f6;border-radius:3px;padding:1px'>▶ {code}</div>",
                            unsafe_allow_html=True)
            else:
                st.markdown(f"<div style='text-align:center;color:#d1d5db'>{icon}</div>"
                            f"<div style='text-align:center;font-size:0.6rem;color:#9ca3af'>{code}</div>",
                            unsafe_allow_html=True)



# ═══════════════════════════════════════════════════════════════════════
# ZERO-STOCK DETECTION
# ═══════════════════════════════════════════════════════════════════════

def _detect_zero_stock_lines(all_lines):
    """
    Returns lines where stock = 0 and route is VENDOR/EXTERNAL_LAB.
    These need BOTH a replenishment PO (restock shelf) AND a client PO (fulfil order).
    """
    zero = []
    for l in all_lines:
        route = str(l.get("manufacturing_route") or "STOCK").upper()
        if route in ("VENDOR", "EXTERNAL_LAB"):
            avail = int(l.get("available_stock") or l.get("stock_qty") or 0)
            alloc = int(l.get("allocated_qty") or 0)
            if avail <= 0 and alloc <= 0:
                zero.append(l)
    return zero


def _create_replenishment_po(order, lines):
    """Create a REPLENISHMENT type PO — restocks the shelf, not linked to client billing."""
    try:
        from modules.sql_adapter import run_write, run_query, run_scalar
        import uuid as _uuid

        order_id = order.get("order_no") or str(order.get("id", ""))

        # Resolve effective supplier from product master (override → preferred → fallback)
        supplier_id   = lines[0].get("supplier_id") if lines else None
        supplier_name = lines[0].get("supplier_name", "Unknown") if lines else "Unknown"
        try:
            from modules.procurement.po_engine import get_effective_supplier
            pid_first = str(lines[0].get("product_id") or "") if lines else ""
            if pid_first:
                eff = get_effective_supplier(pid_first)
                if eff.get("supplier_id"):
                    supplier_id   = eff["supplier_id"]
                    supplier_name = eff["supplier_name"] or supplier_name
        except Exception:
            pass

        # Insert replenishment PO
        _sync_supplier_orders_id_sequence()
        po_id = run_scalar("""
            INSERT INTO supplier_orders (
                supplier_order_id, customer_order_id, supplier_id, supplier_name,
                order_date, status, po_type, total_value, priority, notes
            ) VALUES (
                %(ref)s, %(cid)s, %(sid)s, %(sname)s,
                CURRENT_DATE, 'DRAFT', 'REPLENISHMENT',
                %(val)s, 'NORMAL', 'Auto-created: zero-stock replenishment'
            ) RETURNING id
        """, {
            "ref":   f"REPL-{order_id[:8]}",
            "cid":   order_id,
            "sid":   supplier_id,
            "sname": supplier_name,
            "val":   sum(float(l.get("unit_price") or 0) * int(l.get("billing_qty") or 1) for l in lines),
        })

        if not po_id:
            return None

        run_write("""
            UPDATE supplier_orders SET supplier_order_id=%(ref)s WHERE id=%(id)s
        """, {"ref": f"REPL-{po_id}", "id": po_id})

        _populate_po_items(po_id, lines, run_write)
        return f"REPL-{po_id}"
    except Exception as e:
        st.error(f"Replenishment PO failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# EXTERNAL LAB PO CREATION
# ═══════════════════════════════════════════════════════════════════════

def _populate_po_items(po_id, lines, run_write):
    """Insert items into an existing PO header. Used by create and orphan-repair."""
    for i, l in enumerate(lines, 1):
        _qty = int(l.get("billing_qty") or 1)
        run_write("""
            INSERT INTO supplier_order_items (
                supplier_order_id, item_no, product_id, product_name,
                eye_side, sph, cyl, axis, add_power,
                ordered_qty, received_qty, pending_qty,
                unit_price, total_price,
                customer_line_id, item_status
            ) VALUES (
                %(po)s, %(no)s, %(pid)s, %(pname)s,
                %(eye)s, %(sph)s, %(cyl)s, %(ax)s, %(add)s,
                %(qty)s, 0, %(qty)s,
                %(up)s, %(tp)s, %(clid)s, 'PENDING'
            )
        """, {
            "po":    po_id, "no": i,
            "pid":   str(l.get("product_id") or ""),
            "pname": l.get("product_name", ""),
            "eye":   l.get("eye_side", ""),
            "sph":   l.get("sph") if l.get("sph") is not None else None,
            "cyl":   l.get("cyl") if l.get("cyl") is not None else None,
            "ax":    l.get("axis") if l.get("axis") not in (None, 0, "0") else None,
            "add":   l.get("add_power") if l.get("add_power") is not None else None,
            "qty":   _qty,
            "up":    float(l.get("unit_price") or 0),
            "tp":    float(l.get("billing_total") or 0),
            "clid":  str(l.get("id") or "") or None,
        })


def _create_external_lab_order(order, lab_lines):
    """Create a PO for EXTERNAL_LAB lines.
    If an orphaned PO (header with 0 items) already exists, populates it instead of
    creating a new header — handles the case where a previous attempt crashed mid-insert.
    """
    try:
        from modules.sql_adapter import run_write, run_scalar, run_query
        order_id      = order.get("order_no") or str(order.get("id", ""))
        supplier_id   = lab_lines[0].get("supplier_id") if lab_lines else None
        supplier_name = lab_lines[0].get("supplier_name", "External Lab") if lab_lines else "External Lab"

        # Repair orphaned PO (header exists, 0 items — from a previous failed insert)
        try:
            orphan = run_query("""
                SELECT so.id FROM supplier_orders so
                WHERE so.customer_order_id = %(cid)s AND so.po_type = 'EXTERNAL_LAB'
                  AND NOT EXISTS (
                      SELECT 1 FROM supplier_order_items si WHERE si.supplier_order_id = so.id
                  )
                LIMIT 1
            """, {"cid": order_id})
        except Exception:
            orphan = []

        if orphan:
            po_id = int(orphan[0]["id"])
            _populate_po_items(po_id, lab_lines, run_write)
            run_write("UPDATE supplier_orders SET total_value=%(v)s WHERE id=%(id)s", {
                "v":  sum(float(l.get("unit_price") or 0) * int(l.get("billing_qty") or 1) for l in lab_lines),
                "id": po_id,
            })
            return f"LAB-{po_id}"

        # Create fresh PO header
        _sync_supplier_orders_id_sequence()
        po_id = run_scalar("""
            INSERT INTO supplier_orders (
                supplier_order_id, customer_order_id, supplier_id, supplier_name,
                order_date, status, po_type, total_value, priority
            ) VALUES (
                %(ref)s, %(cid)s, %(sid)s, %(sname)s,
                CURRENT_DATE, 'DRAFT', 'EXTERNAL_LAB', %(val)s, 'NORMAL'
            ) RETURNING id
        """, {
            "ref":   f"LAB-{order_id[:8]}",
            "cid":   order_id,
            "sid":   supplier_id,
            "sname": supplier_name,
            "val":   sum(float(l.get("unit_price") or 0) * int(l.get("billing_qty") or 1) for l in lab_lines),
        })
        if not po_id:
            return None

        run_write("UPDATE supplier_orders SET supplier_order_id=%(ref)s WHERE id=%(id)s",
                  {"ref": f"LAB-{po_id}", "id": po_id})

        _populate_po_items(po_id, lab_lines, run_write)
        return f"LAB-{po_id}"
    except Exception as e:
        st.error(f"Lab PO creation failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# UNIFIED SUPPLIER PANEL — replaces old render_supplier_panel
# ═══════════════════════════════════════════════════════════════════════

def render_supplier_panel(order: Dict) -> None:
    """
    Unified procurement panel — all 3 trains managed here:
      Train C — External Lab Orders
      Train D — Supplier RX Orders (VENDOR)
      Train E — Zero-Stock: Replenishment + Client PO

    All trains write ready_qty to order_lines on receipt.
    Billing junction unlocks when all lines ready_qty >= billing_qty.
    """
    import streamlit as st
    order_id  = order.get("order_no") or str(order.get("id", ""))
    all_lines = (order.get("stock_lines", []) +
                 order.get("inhouse_lines", []) +
                 order.get("lab_order_lines", []))

    vendor_lines   = [l for l in all_lines if str(l.get("manufacturing_route","")).upper() == "VENDOR"]
    ext_lab_lines  = [l for l in all_lines if str(l.get("manufacturing_route","")).upper() == "EXTERNAL_LAB"]
    zero_stk_lines = _detect_zero_stock_lines(all_lines)

    # ── Fetch all POs for this order ─────────────────────────────────
    try:
        from modules.sql_adapter import run_query as _rq
        # Fetch po_type RAW (no COALESCE) so the reclassify loop below can see
        # truly NULL values and fix them before the tab split happens.
        all_pos = _rq("""
            SELECT so.id, so.supplier_order_id, so.supplier_name, so.status,
                   so.po_type,
                   so.order_date, so.expected_delivery_date,
                   COALESCE(SUM(soi.ordered_qty),0)  AS total_ordered,
                   COALESCE(SUM(soi.received_qty),0) AS total_received
            FROM supplier_orders so
            LEFT JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
            WHERE so.customer_order_id = %(oid)s
              AND COALESCE(so.status, '') != 'CANCELLED'
            GROUP BY so.id, so.supplier_order_id, so.supplier_name,
                     so.status, so.po_type, so.order_date, so.expected_delivery_date
            ORDER BY so.id
        """, {"oid": order_id}) or []
    except Exception:
        all_pos = []

    # ── Fix stale po_type=NULL POs ──────────────────────────────────────────
    # POs created before po_type column existed have po_type=NULL → show as CLIENT_ORDER.
    # Detect if the order's current DB lines are EXTERNAL_LAB — if so, reclassify
    # any NULL-type PO as EXTERNAL_LAB (it was created for those lines).
    try:
        from modules.sql_adapter import run_query as _rqt
        _db_routes = _rqt("""
            SELECT UPPER(COALESCE(
                ol.lens_params::jsonb->>'manufacturing_route', ''
            )) AS route, COUNT(*) AS n
            FROM order_lines ol JOIN orders o ON o.id = ol.order_id
            WHERE o.order_no = %(oid)s AND COALESCE(ol.is_deleted,FALSE)=FALSE
            GROUP BY 1
        """, {"oid": order_id}) or []
        _db_route_map = {r["route"]: int(r["n"]) for r in _db_routes}
    except Exception:
        _db_route_map = {}

    _order_is_ext_lab = _db_route_map.get("EXTERNAL_LAB", 0) > 0
    _order_is_vendor  = _db_route_map.get("VENDOR", 0) > 0

    # ── Reclassify NULL-type POs permanently ───────────────────────────
    # Raw fetch above gives po_type=None for rows created before the column existed.
    # Decide type from actual DB order lines and write it back once.
    try:
        from modules.sql_adapter import run_write as _rwt2
        for _p in all_pos:
            if _p.get("po_type") is None:
                # Determine best type from DB line routes
                if _order_is_ext_lab and not _order_is_vendor:
                    _new_type = "EXTERNAL_LAB"
                elif _order_is_vendor and not _order_is_ext_lab:
                    _new_type = "CLIENT_ORDER"
                else:
                    # Mixed or unknown — leave as CLIENT_ORDER (safe default)
                    _new_type = "CLIENT_ORDER"
                _p["po_type"] = _new_type
                try:
                    _rwt2("UPDATE supplier_orders SET po_type=%(t)s WHERE id=%(id)s",
                          {"t": _new_type, "id": _p["id"]})
                except Exception:
                    pass
    except Exception:
        pass

    # Tab split — now using the corrected/reclassified po_type values
    vendor_pos  = [p for p in all_pos if (p.get("po_type") or "CLIENT_ORDER") == "CLIENT_ORDER"]
    lab_pos     = [p for p in all_pos if p.get("po_type") == "EXTERNAL_LAB"]
    repl_pos    = [p for p in all_pos if p.get("po_type") == "REPLENISHMENT"]

    stages = _fetch_supplier_stages()

    # ── Zero-stock banner ─────────────────────────────────────────────
    if zero_stk_lines:
        st.markdown(
            f"<div style='background:#2d1a00;border:1px solid #f59e0b;"
            f"border-radius:8px;padding:10px 14px;margin-bottom:12px'>"
            f"<b style='color:#f59e0b'>⚠️ Zero-Stock Items Detected</b>"
            f"<div style='color:#fcd34d;font-size:0.8rem;margin-top:4px'>"
            f"{len(zero_stk_lines)} line(s) have no stock. "
            f"You can create a <b>Replenishment PO</b> (restock shelf) "
            f"separately from the Client Order PO (fulfil this order).</div></div>",
            unsafe_allow_html=True,
        )

    # ── Tabs: Client Orders | External Lab | Replenishment ────────────
    _tab_labels = ["🏭 Supplier Orders", "🔬 External Lab", "♻️ Replenishment"]
    t_vendor, t_lab, t_repl = st.tabs(_tab_labels)

    # ═══════════ TAB 1 — SUPPLIER (VENDOR) ORDERS ════════════════════
    with t_vendor:
        # Stale check: query DB directly so in-memory route loss doesn't give false warnings
        _vendor_stale = False
        if vendor_pos and not vendor_lines:
            try:
                from modules.sql_adapter import run_query as _rqs
                _db_vendor = _rqs("""
                    SELECT COUNT(*) AS n FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE o.order_no = %(oid)s
                      AND UPPER(COALESCE(
                            ol.lens_params::jsonb->>'manufacturing_route',''
                          )) = 'VENDOR'
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                """, {"oid": order_id})
                _vendor_stale = not (_db_vendor and int(_db_vendor[0].get("n", 0)) > 0)
            except Exception:
                _vendor_stale = False
        if _vendor_stale:
            st.warning(
                "⚠️ **Route changed.** These Supplier orders were created when lines "
                "were routed to a Supplier, but the assignment has since been changed. "
                "Cancel the stale PO(s) below to keep records clean."
            )
        # Only offer "Create Supplier Order" if DB actually has VENDOR lines.
        # Session-state vendor_lines can be stale — trust the DB route map.
        _vendor_lines_to_create = vendor_lines if _order_is_vendor else []
        _render_po_list(vendor_pos, stages, order_id, order,
                        po_type_label="Supplier",
                        create_label="📦 Create Supplier Order",
                        lines_to_create=_vendor_lines_to_create,
                        create_fn=_create_vendor_po,
                        allow_cancel=_vendor_stale)

    # ═══════════ TAB 2 — EXTERNAL LAB ORDERS ════════════════════════
    with t_lab:
        # Stale check: query DB directly — don't trust in-memory ext_lab_lines
        _lab_stale = False
        if lab_pos and not ext_lab_lines:
            try:
                from modules.sql_adapter import run_query as _rql
                _db_lab = _rql("""
                    SELECT COUNT(*) AS n FROM order_lines ol
                    JOIN orders o ON o.id = ol.order_id
                    WHERE o.order_no = %(oid)s
                      AND UPPER(COALESCE(
                            ol.lens_params::jsonb->>'manufacturing_route',''
                          )) = 'EXTERNAL_LAB'
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                """, {"oid": order_id})
                _lab_stale = not (_db_lab and int(_db_lab[0].get("n", 0)) > 0)
            except Exception:
                _lab_stale = False
        if _lab_stale:
            st.warning(
                "⚠️ **Route changed.** These Lab orders were created when lines were "
                "routed to External Lab, but the assignment has since changed. "
                "Cancel stale PO(s) below."
            )
        _render_po_list(lab_pos, stages, order_id, order,
                        po_type_label="External Lab",
                        create_label="🔬 Create Lab Order",
                        lines_to_create=ext_lab_lines,
                        create_fn=_create_external_lab_order,
                        allow_cancel=_lab_stale)

    # ═══════════ TAB 3 — REPLENISHMENT ════════════════════════════════
    with t_repl:
        if not zero_stk_lines and not repl_pos:
            st.info("✅ No zero-stock items — no replenishment needed for this order.")
        else:
            _render_po_list(repl_pos, stages, order_id, order,
                            po_type_label="Replenishment",
                            create_label="♻️ Create Replenishment PO",
                            lines_to_create=zero_stk_lines,
                            create_fn=_create_replenishment_po,
                            note="Replenishment POs restock your shelf. "
                                 "They are separate from the client order PO "
                                 "and do NOT affect the billing readiness of this order.")


def _create_vendor_po(order, lines):
    """Create a CLIENT_ORDER PO for VENDOR-routed lines.
    Does NOT call create_supplier_order_from_lines because that function has a
    broad existing-PO guard that fires when any PO (e.g. EXTERNAL_LAB) exists
    for the order, silently blocking creation.
    This function is scoped to po_type='CLIENT_ORDER' only.
    """
    try:
        from modules.sql_adapter import run_write, run_scalar, run_query
        import datetime
        order_id      = order.get("order_no") or str(order.get("id", ""))
        # Use supplier_id from the line if set; otherwise fall back to first active supplier
        supplier_id   = lines[0].get("supplier_id") if lines else None
        supplier_name = lines[0].get("supplier_name", "") if lines else ""
        if not supplier_id:
            sup = run_query(
                "SELECT id, party_name FROM parties WHERE party_type='Supplier' AND is_active=TRUE ORDER BY party_name LIMIT 1",
                {}
            ) or []
            supplier_id   = sup[0]["id"]        if sup else None
            supplier_name = sup[0]["party_name"] if sup else "Unknown Supplier"

        # Repair orphaned CLIENT_ORDER PO (header with 0 items)
        try:
            orphan = run_query("""
                SELECT so.id FROM supplier_orders so
                WHERE so.customer_order_id = %(cid)s
                  AND COALESCE(so.po_type,'CLIENT_ORDER') = 'CLIENT_ORDER'
                  AND NOT EXISTS (
                      SELECT 1 FROM supplier_order_items si WHERE si.supplier_order_id = so.id
                  )
                LIMIT 1
            """, {"cid": order_id})
        except Exception:
            orphan = []

        if orphan:
            po_id = int(orphan[0]["id"])
            _populate_po_items(po_id, lines, run_write)
            return f"SO-{po_id}"

        # Guard: CLIENT_ORDER already exists with items → don't duplicate
        try:
            dup = run_query("""
                SELECT so.id FROM supplier_orders so
                WHERE so.customer_order_id = %(cid)s
                  AND COALESCE(so.po_type,'CLIENT_ORDER') = 'CLIENT_ORDER'
                  AND EXISTS (SELECT 1 FROM supplier_order_items si WHERE si.supplier_order_id = so.id)
                LIMIT 1
            """, {"cid": order_id})
        except Exception:
            dup = []
        if dup:
            st.warning("⚠️ A Supplier order already exists for this order.")
            return None

        # ── Use preferred supplier + TAT-aware expected delivery ────────
        # get_effective_supplier checks manual override first, then preferred_supplier_id
        try:
            from modules.procurement.po_engine import (
                get_effective_supplier, calculate_expected_delivery
            )
            pid_first = str(lines[0].get("product_id") or "") if lines else ""
            eff = get_effective_supplier(pid_first) if pid_first else {}
            if eff.get("supplier_id"):
                supplier_id   = eff["supplier_id"]
                supplier_name = eff["supplier_name"] or supplier_name
            tat = int(eff.get("tat_days") or 1)
            exp_iso = calculate_expected_delivery(
                supplier_id  = supplier_id,
                placement_dt = datetime.datetime.now(),
                tat_days     = tat,
            )
            expected = datetime.date.fromisoformat(exp_iso) if exp_iso else                        (datetime.datetime.now() + datetime.timedelta(days=tat)).date()
        except Exception:
            expected = (datetime.datetime.now() + datetime.timedelta(days=7)).date()

        _sync_supplier_orders_id_sequence()
        po_id = run_scalar("""
            INSERT INTO supplier_orders (
                supplier_order_id, supplier_id, supplier_name, customer_order_id,
                order_date, expected_delivery_date, status, po_type,
                total_items, total_qty, total_value,
                created_by, created_at, updated_at
            ) VALUES (
                'SO-PENDING', %(sid)s, %(sname)s, %(cid)s,
                NOW(), %(exp)s, 'DRAFT', 'CLIENT_ORDER',
                %(ti)s, %(tq)s, %(tv)s,
                'backoffice', NOW(), NOW()
            ) RETURNING id
        """, {
            "sid":   supplier_id,
            "sname": supplier_name,
            "cid":   order_id,
            "exp":   expected,
            "ti":    len(lines),
            "tq":    sum(int(l.get("billing_qty") or 1) for l in lines),
            "tv":    sum(float(l.get("unit_price") or 0) * int(l.get("billing_qty") or 1) for l in lines),
        })
        if not po_id:
            st.error("❌ Failed to create supplier order in DB.")
            return None

        run_write("UPDATE supplier_orders SET supplier_order_id=%(ref)s WHERE id=%(id)s",
                  {"ref": f"SO-{po_id}", "id": po_id})
        _populate_po_items(po_id, lines, run_write)
        return f"SO-{po_id}"
    except Exception as e:
        st.error(f"Supplier PO failed: {e}")
        return None


def _render_po_list(pos, stages, order_id, order, po_type_label,
                    create_label, lines_to_create, create_fn,
                    note=None, allow_cancel=False):
    """Render PO list with timeline for a given po_type bucket.
    allow_cancel=True shows a Cancel button on DRAFT POs (for stale/wrong-type POs).
    """
    import streamlit as st

    if note:
        st.info(note)

    if not pos:
        if lines_to_create:
            st.info(f"📊 {len(lines_to_create)} item(s) awaiting {po_type_label} order.")
            if st.button(create_label, type="primary",
                         key=f"create_{po_type_label}_{order_id}",
                         use_container_width=True):
                ref = create_fn(order, lines_to_create)
                if ref:
                    st.success(f"✅ {po_type_label} order created: {ref}")
                    st.rerun()
        else:
            st.info(f"💡 No {po_type_label} items in this order.")
        return

    for po in pos:
        po_id_int      = int(po.get("id", 0))
        po_ref         = po.get("supplier_order_id", str(po_id_int))
        supplier_name  = po.get("supplier_name", "Unknown")
        status         = po.get("status", "DRAFT")
        expected       = str(po.get("expected_delivery_date") or "")[:10]
        total_ordered  = int(po.get("total_ordered") or 0)
        total_received = int(po.get("total_received") or 0)

        icon  = STAGE_ICONS.get(status, "📋")
        color = STAGE_COLORS.get(status, "#6b7280")

        with st.container(border=True):
            h1, h2, h3 = st.columns([4, 2, 1])
            with h1:
                st.markdown(f"**{supplier_name}** &nbsp;"
                            f"<span style='font-size:0.8rem;color:#6b7280;"
                            f"font-family:monospace'>{po_ref}</span>",
                            unsafe_allow_html=True)
                if expected:
                    st.caption(f"Expected: {expected}")
            with h2:
                pct = int(100 * total_received / total_ordered) if total_ordered else 0
                st.progress(pct / 100)
                st.caption(f"{total_received}/{total_ordered} received ({pct}%)")
            with h3:
                st.markdown(
                    f"<div style='text-align:center;padding:4px 0'>"
                    f"<span style='background:{color};color:#fff;border-radius:5px;"
                    f"padding:3px 8px;font-size:0.75rem;font-weight:700'>"
                    f"{icon} {status}</span></div>",
                    unsafe_allow_html=True)

            if stages:
                _render_timeline(stages, status)

            items = _fetch_po_items(po_id_int)

            # ── Orphan: header exists but 0 items (visible outside expander) ────
            if not items and status == "DRAFT":
                # Always try to fetch lines from DB — don't rely on session state
                # (session lines may have stale/missing manufacturing_route)
                _repair_lines = lines_to_create or []
                if not _repair_lines:
                    try:
                        from modules.sql_adapter import run_query as _rqr
                        _db_lines = _rqr("""
                            SELECT ol.id, ol.product_name, ol.eye_side,
                                   ol.sph, ol.cyl, ol.axis, ol.add_power,
                                   ol.billing_qty, ol.unit_price,
                                   (ol.unit_price * ol.billing_qty) AS billing_total,
                                   ol.product_id
                            FROM order_lines ol
                            JOIN orders o ON o.id = ol.order_id
                            WHERE o.order_no = %(oid)s
                              AND UPPER(COALESCE(
                                    ol.lens_params::jsonb->>'manufacturing_route',
                                    ''
                                  )) IN ('EXTERNAL_LAB','VENDOR')
                        """, {"oid": order_id}) or []
                        _repair_lines = _db_lines
                    except Exception:
                        _repair_lines = []

                st.warning("⚠️ This PO has no items — the previous creation was interrupted.")
                rc1, rc2 = st.columns(2)
                with rc1:
                    _btn_label = f"🔧 Populate Items ({len(_repair_lines)})" if _repair_lines else "🔧 Populate Items"
                    _btn_disabled = not bool(_repair_lines)
                    if st.button(_btn_label, key=f"repair_{po_id_int}",
                                 type="primary", use_container_width=True,
                                 disabled=_btn_disabled,
                                 help="Re-run item insert using current order lines"):
                        try:
                            from modules.sql_adapter import run_write as _rw
                            _populate_po_items(po_id_int, _repair_lines, _rw)
                            from modules.sql_adapter import run_write as _rw2
                            _rw2("UPDATE supplier_orders SET total_value=%(v)s WHERE id=%(id)s", {
                                "v": sum(float(l.get("unit_price") or 0) * int(l.get("billing_qty") or 1) for l in _repair_lines),
                                "id": po_id_int,
                            })
                            st.success(f"✅ {len(_repair_lines)} item(s) added to PO")
                            st.rerun()
                        except Exception as _err:
                            st.error(f"Repair failed: {_err}")
                    if _btn_disabled:
                        st.caption("No matching lines found in DB — cancel this PO.")
                with rc2:
                    if st.button("🗑 Cancel this PO", key=f"cancel_orphan_{po_id_int}",
                                 use_container_width=True):
                        try:
                            from modules.sql_adapter import run_write as _rw3
                            _rw3("UPDATE supplier_orders SET status='CANCELLED' WHERE id=%(id)s",
                                 {"id": po_id_int})
                            st.success("✅ PO cancelled")
                            st.rerun()
                        except Exception as _err:
                            st.error(f"Cancel failed: {_err}")
                continue   # skip expander — no stages/receipt without items

            # ── Cancel button for stale POs that have items (route changed after creation) ──
            if allow_cancel and not lines_to_create and status in ("DRAFT", "SENT"):
                cc1, cc2 = st.columns([3, 1])
                with cc2:
                    if st.button("🗑 Cancel PO", key=f"cancel_po_{po_id_int}",
                                 use_container_width=True,
                                 help="Route was changed — cancel this PO"):
                        try:
                            from modules.sql_adapter import run_write as _rw5
                            _rw5("UPDATE supplier_orders SET status='CANCELLED' WHERE id=%(id)s",
                                 {"id": po_id_int})
                            st.success("✅ PO cancelled")
                            st.rerun()
                        except Exception as _err:
                            st.error(f"Cancel failed: {_err}")

            # Stage advance + receipt UI (collapsed unless actionable)
            allowed_next = _fetch_allowed_next(status)
            can_receive  = "PARTIAL" in allowed_next or "RECEIVED" in allowed_next

            with st.expander(f"📋 Items & Actions ({len(items)} items)", expanded=can_receive):
                _render_po_items_rl(items)

                # ── 🔗 Bonzer Autofill Link ──────────────────────────────────
                # Shown when supplier is Bonzer Lenses. Staff click → Bonzer
                # order form opens with all fields pre-filled via Tampermonkey.
                # Staff only need to pick Dealer / Master Brand / Price → Save.
                if "bonzer" in supplier_name.lower():
                    _bonzer_url = _build_bonzer_autofill_url(order, items)
                    st.markdown(
                        f"<div style='background:#0f1e38;border:1px solid #1e3a5f;"
                        f"border-left:4px solid #f59e0b;border-radius:8px;"
                        f"padding:10px 14px;margin:10px 0'>"
                        f"<div style='color:#fbbf24;font-weight:700;font-size:0.82rem'>"
                        f"🔗 Send to Bonzer Portal</div>"
                        f"<div style='color:#94a3b8;font-size:0.72rem;margin-top:3px'>"
                        f"Opens Bonzer order form with Order No., R/L Rx and customer "
                        f"pre-filled. Pick Dealer / Master Brand / Price, then Save.</div>"
                        f"<div style='margin-top:8px'>"
                        f"<a href='{_bonzer_url}' target='_blank' "
                        f"style='background:#f59e0b;color:#000;font-weight:700;"
                        f"padding:6px 16px;border-radius:6px;text-decoration:none;"
                        f"font-size:0.82rem'>📤 Open Bonzer Form (Autofill)</a>"
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )

                # ── Purchase link + READY_TO_BILL shortcut ──────────────────
                # When PO is RECEIVED or in INSPECTION/COMPLETE, show the
                # linked purchase ref and allow advancing to READY_TO_BILL.
                if status in ("RECEIVED", "INSPECTION", "COMPLETE", "READY_TO_BILL"):
                    _po_ref = po.get("po_ref") or po.get("reference_no") or ""
                    if _po_ref:
                        st.success(f"🔗 Linked to Purchase: **{_po_ref}**")
                    st.markdown("---")
                    if status == "INSPECTION":
                        _ic1, _ic2 = st.columns(2)
                        with _ic1:
                            if st.button("✅ Inspection Passed → Complete",
                                         key=f"insp_pass_{po_id_int}",
                                         type="primary", use_container_width=True):
                                if _advance_po_stage(po_id_int, order_id, "COMPLETE"):
                                    st.success("✅ Inspection passed — PO → COMPLETE")
                                    st.rerun()
                        with _ic2:
                            if st.button("📦 Mark READY TO BILL",
                                         key=f"rtb_{po_id_int}",
                                         use_container_width=True):
                                if _advance_po_stage(po_id_int, order_id, "READY_TO_BILL"):
                                    st.success("✅ PO marked READY TO BILL — billing gate unlocked!")
                                    st.rerun()
                    elif status == "COMPLETE":
                        if st.button("📦 Mark READY TO BILL",
                                     key=f"rtb_complete_{po_id_int}",
                                     type="primary", use_container_width=True):
                            if _advance_po_stage(po_id_int, order_id, "READY_TO_BILL"):
                                st.success("✅ PO marked READY TO BILL — billing gate unlocked!")
                                st.rerun()
                    elif status == "READY_TO_BILL":
                        st.success("✅ READY TO BILL — billing gate is open for this PO")

                if allowed_next and status != "CLOSED" and status not in ("RECEIVED","INSPECTION","COMPLETE","READY_TO_BILL"):
                    st.markdown("**Advance Stage**")
                    acols = st.columns(len(allowed_next))
                    for col, nxt in zip(acols, allowed_next):
                        _icon = STAGE_ICONS.get(nxt, "➡️")
                        if col.button(f"{_icon} → {nxt}", key=f"adv_{po_id_int}_{nxt}",
                                      use_container_width=True):
                            if _advance_po_stage(po_id_int, order_id, nxt):
                                st.success(f"✅ Stage → {nxt}")
                                st.rerun()

                if can_receive and items:
                    st.markdown("**Record Receipt**")

                    pending_items = [it for it in items if it.get("item_status") != "RECEIVED"]

                    if not pending_items:
                        st.success("✅ All items fully received.")
                    else:
                        # ── Quick-action buttons ─────────────────────────────────
                        qa1, qa2 = st.columns(2)

                        # "Receive All" — mark every pending item at full ordered_qty
                        if qa1.button("✅ Receive All Items",
                                      key=f"rcv_all_{po_id_int}",
                                      use_container_width=True,
                                      type="primary"):
                            full_map = {it["id"]: int(it.get("ordered_qty") or 0)
                                        for it in pending_items
                                        if int(it.get("ordered_qty") or 0) > 0}
                            if full_map and _save_received_qtys(po_id_int, full_map):
                                _advance_po_stage(po_id_int, order_id, "RECEIVED")
                                st.success("✅ All items received — PO → RECEIVED")
                                st.rerun()

                        # "Receive R+L Together" — only if exactly 2 pending items (R and L pair)
                        eyes = {str(it.get("eye_side","")).upper() for it in pending_items}
                        has_pair = len(pending_items) == 2 and {"R","L"}.issubset(eyes) or                                    len(pending_items) == 2 and {"RIGHT","LEFT"}.issubset(eyes)
                        if has_pair:
                            if qa2.button("👁👁 Receive R+L Together",
                                          key=f"rcv_pair_{po_id_int}",
                                          use_container_width=True):
                                pair_map = {it["id"]: int(it.get("ordered_qty") or 0)
                                            for it in pending_items
                                            if int(it.get("ordered_qty") or 0) > 0}
                                if pair_map and _save_received_qtys(po_id_int, pair_map):
                                    all_full = all(
                                        pair_map.get(it["id"], 0) >= int(it.get("ordered_qty") or 0)
                                        for it in items
                                    )
                                    _advance_po_stage(po_id_int, order_id,
                                                      "RECEIVED" if all_full else "PARTIAL")
                                    st.success("✅ R+L received")
                                    st.rerun()

                        st.markdown("<div style='color:#475569;font-size:0.72rem;margin:8px 0 4px'>Or enter quantities manually:</div>",
                                    unsafe_allow_html=True)

                        # ── Individual quantity inputs ────────────────────────────
                        received_map = {}
                        for item in pending_items:
                            max_v = int(item.get("ordered_qty") or 0)
                            eye   = str(item.get("eye_side","") or "")
                            eye_icon = "👁R" if eye.upper() in ("R","RIGHT") else                                        "👁L" if eye.upper() in ("L","LEFT") else "👁"
                            val = st.number_input(
                                f"{eye_icon}  {item.get('product_name','')}  ({eye})",
                                min_value=0, max_value=max_v,
                                value=int(item.get("received_qty") or 0),
                                key=f"rcv_{po_id_int}_{item['id']}",
                            )
                            if val > 0:
                                received_map[item["id"]] = val

                        if received_map:
                            if st.button("💾 Save Receipt",
                                         key=f"save_rcv_{po_id_int}",
                                         type="primary",
                                         use_container_width=True):
                                if _save_received_qtys(po_id_int, received_map):
                                    all_full = all(
                                        received_map.get(it["id"], 0) >= int(it.get("ordered_qty") or 0)
                                        for it in items
                                    )
                                    next_s = "RECEIVED" if all_full else "PARTIAL"
                                    _advance_po_stage(po_id_int, order_id, next_s)
                                    st.success(f"✅ Receipt saved — PO → {next_s}")
                                    st.rerun()

    # Offer to create additional PO if lines not yet covered
    if lines_to_create:
        st.markdown("---")
        if st.button(f"➕ Create Another {po_type_label} Order",
                     key=f"create_more_{po_type_label}_{order_id}"):
            ref = create_fn(order, lines_to_create)
            if ref:
                st.success(f"✅ {po_type_label} order created: {ref}")
                st.rerun()


def _render_po_items_rl(items):
    """Render PO items R/L side by side."""
    if not items:
        st.caption("No items in this order.")
        return
    groups: dict = {}
    solo = []
    for it in items:
        pid = str(it.get("product_id") or it.get("product_name", ""))
        eye = str(it.get("eye_side") or "").upper().strip()
        if pid not in groups:
            groups[pid] = {"name": it.get("product_name",""), "R": None, "L": None, "solo": []}
        if eye in ("R","RIGHT") and groups[pid]["R"] is None:
            groups[pid]["R"] = it
        elif eye in ("L","LEFT") and groups[pid]["L"] is None:
            groups[pid]["L"] = it
        else:
            groups[pid]["solo"].append(it)

    for grp in groups.values():
        pairs = [(grp["R"], grp["L"])] if (grp["R"] or grp["L"]) else []
        for s in grp["solo"]:
            pairs.append((s, None))
        for r_it, l_it in pairs:
            c1, c2 = st.columns(2)
            for col, it in [(c1, r_it), (c2, l_it)]:
                if it is None:
                    col.empty()
                    continue
                eye_lbl = "👁 RE" if str(it.get("eye_side","")).upper() in ("R","RIGHT") else "👁 LE"
                stat    = it.get("item_status","PENDING")
                sc      = {"RECEIVED":"#10b981","PARTIAL":"#f59e0b","PENDING":"#6b7280"}.get(stat,"#6b7280")
                pwr_parts = []
                if it.get("sph") is not None: pwr_parts.append(f"SPH {float(it['sph']):+.2f}")
                if it.get("cyl") is not None: pwr_parts.append(f"CYL {float(it['cyl']):+.2f}")
                if it.get("axis"):            pwr_parts.append(f"AX {int(it['axis'])}°")
                if it.get("add_power") not in (None, 0): pwr_parts.append(f"ADD {float(it['add_power']):+.2f}")
                pwr = "  ".join(pwr_parts)
                rcv = int(it.get("received_qty") or 0)
                ord_ = int(it.get("ordered_qty") or 0)
                col.markdown(
                    f"<div style='background:#1e293b;border-radius:6px;padding:8px 10px;"
                    f"border-left:3px solid {sc};margin:2px 0'>"
                    f"<b style='color:#e2e8f0'>{eye_lbl}</b> "
                    f"<span style='color:#94a3b8;font-size:0.75rem'>{it.get('product_name','')}</span><br>"
                    f"<span style='color:#64748b;font-size:0.7rem'>{pwr}</span><br>"
                    f"<span style='color:{sc};font-size:0.72rem;font-weight:700'>{stat}</span> "
                    f"<span style='color:#94a3b8;font-size:0.72rem'>{rcv}/{ord_} pcs</span>"
                    f"</div>",
                    unsafe_allow_html=True)
