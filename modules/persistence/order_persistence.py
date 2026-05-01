"""
Order Persistence Layer
Safe transactional save for backoffice orders

RULES:
- DB is source of truth
- UI never writes partial state
- Replace lines atomically
- Self-healing migrations: auto-add missing columns on first save

REAL DB SCHEMA (confirmed from working SELECTs in order_repository + sql_adapter):
  orders (confirmed-real columns):
    id, order_no, order_type, order_source, status, total_items, total_value,
    created_at, party_name, patient_name, patient_mobile, customer_order_no

  orders (optional — auto-added by _ensure_orders_columns on first save):
    party_id    UUID          — FK to parties; NULL for retail/walk-in
    created_by  TEXT          — operator who created the order
    updated_at  TIMESTAMPTZ   — last mutation timestamp
    updated_by  TEXT          — operator who last updated

  order_lines (confirmed-real columns):
    id, order_id, product_id, sph, cyl, axis, add_power, eye_side,
    quantity, unit_price, total_price, status, lens_params, boxing_params,
    allocated_qty, ready_qty, billed_qty, dispatched_qty

  order_lines (optional — auto-added by _ensure_line_pricing_columns):
    gst_percent     NUMERIC(5,2)
    gst_amount      NUMERIC(12,2)
    discount_percent NUMERIC(5,2)
    discount_amount  NUMERIC(12,2)

UI field aliases (never stored directly):
  billing_qty   -> DB: quantity
  billing_total -> DB: total_price
  manufacturing_route -> stored inside lens_params JSONB
  product_name  -> NOT stored (hydrated from products JOIN on load)
"""

import json
import uuid
import logging
import datetime
from typing import Dict

from modules.sql_adapter import get_connection as get_conn

logger = logging.getLogger(__name__)


# ==========================================================
# MIGRATION STATE CACHE  (checked once per process)
# ==========================================================

_ORDERS_EXTRA_COLS_EXIST: bool | None = None   # party_id, created_by, updated_at, updated_by
_LINE_PRICING_COLS_EXIST: bool | None = None   # gst_percent, gst_amount, discount_*


def _ensure_orders_columns(cursor) -> bool:
    """
    Check if the 'extra' orders columns exist.  Auto-create them if missing.
    Idempotent — safe to call on every save.

    Root cause of the original crash:
      The INSERT was unconditionally referencing party_id, created_by,
      updated_at, updated_by — columns that don't exist in the live DB.
      PostgreSQL throws "column X of relation orders does not exist"
      the moment any of those names appear in an INSERT column list.

    This function runs ALTER TABLE ... ADD COLUMN IF NOT EXISTS so the
    second save (and all subsequent ones) use the full INSERT form.
    On the *very first* save the fallback INSERT (only the 12 guaranteed
    columns) is used, the migration runs inside the same transaction,
    and subsequent saves automatically pick up the full form.
    """
    global _ORDERS_EXTRA_COLS_EXIST
    if _ORDERS_EXTRA_COLS_EXIST is not None:
        return _ORDERS_EXTRA_COLS_EXIST

    try:
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name  = 'orders'
               AND column_name = 'party_id'
        """)
        exists = cursor.fetchone()[0] > 0

        if not exists:
            logger.info("[order_persistence] Adding missing columns to orders table")
            cursor.execute("""
                ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS party_id    UUID          DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS created_by  TEXT          DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ   DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS updated_by  TEXT          DEFAULT NULL
            """)
            logger.info("[order_persistence] orders table columns added: party_id, created_by, updated_at, updated_by")

        _ORDERS_EXTRA_COLS_EXIST = True
        return True

    except Exception as exc:
        logger.warning(f"[order_persistence] Could not verify/create orders columns: {exc}")
        _ORDERS_EXTRA_COLS_EXIST = False
        return False


def _ensure_line_pricing_columns(cursor) -> bool:
    """
    Check if gst_percent / gst_amount / discount_* exist on order_lines.
    Auto-create if missing.  Same pattern as order_repository.py.
    """
    global _LINE_PRICING_COLS_EXIST
    if _LINE_PRICING_COLS_EXIST is not None:
        return _LINE_PRICING_COLS_EXIST

    try:
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
             WHERE table_name  = 'order_lines'
               AND column_name = 'gst_amount'
        """)
        exists = cursor.fetchone()[0] > 0

        if not exists:
            logger.info("[order_persistence] Adding pricing columns to order_lines")
            cursor.execute("""
                ALTER TABLE order_lines
                    ADD COLUMN IF NOT EXISTS gst_percent      NUMERIC(5,2)  DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS gst_amount       NUMERIC(12,2) DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS discount_percent  NUMERIC(5,2)  DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS discount_amount   NUMERIC(12,2) DEFAULT 0
            """)
            logger.info("[order_persistence] order_lines pricing columns added")

        _LINE_PRICING_COLS_EXIST = True
        return True

    except Exception as exc:
        logger.warning(f"[order_persistence] Could not verify/create line pricing cols: {exc}")
        _LINE_PRICING_COLS_EXIST = False
        return False


# ==========================================================
# AUDIT HELPER
# ==========================================================

def _audit_order(action: str, order_id: str, order_no: str, payload: dict):
    """Fire-and-forget audit — never raises."""
    try:
        from modules.backoffice.audit_logger import audit, AuditAction
        audit(AuditAction(action), entity="orders", entity_id=order_id,
              payload={"order_no": order_no, **payload})
    except Exception:
        pass


# ==========================================================
# MAIN SAVE FUNCTION
# ==========================================================

def save_order_to_db(order: Dict):
    """
    Safely persist order + lines using a transaction.

    Strategy:
      1. Self-healing migration  (adds missing DB columns, once per process)
      2. Resolve order_id        (reuse existing / lookup by order_no / new UUID)
      3. Upsert order header     (full INSERT when all cols exist; safe fallback otherwise)
      4. Delete existing lines   (atomic replace)
      5. Insert fresh lines      (with GST/discount cols when available)
      6. Commit
      7. Audit log
    """

    conn = get_conn()
    cur  = conn.cursor()

    try:
        # ── 1. Self-healing migrations ─────────────────────────────────
        has_extra_order_cols = _ensure_orders_columns(cur)
        has_line_pricing     = _ensure_line_pricing_columns(cur)

        # ── 2. Resolve order_id ────────────────────────────────────────
        import re as _re_oid
        _raw_oid = str(order.get("order_id") or order.get("id") or "").strip()
        # Accept only real UUIDs (32-36 hex chars with optional dashes)
        # Reject order_no strings like CONS-..., RO-..., PO-..., EDIT-...
        _is_uuid = bool(_raw_oid and _re_oid.match(
            r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$',
            _raw_oid, _re_oid.I
        ))
        order_id = _raw_oid if _is_uuid else ""

        if not order_id and order.get("order_no"):
            try:
                cur.execute(
                    "SELECT id FROM orders WHERE order_no = %s LIMIT 1",
                    (order.get("order_no"),)
                )
                existing = cur.fetchone()
                if existing:
                    order_id = str(existing[0])
            except Exception:
                pass

        # Also try looking up by the raw value as order_no (handles CONS-... passed as order_id)
        if not order_id and _raw_oid and not _is_uuid:
            try:
                cur.execute(
                    "SELECT id FROM orders WHERE order_no = %s LIMIT 1",
                    (_raw_oid,)
                )
                existing = cur.fetchone()
                if existing:
                    order_id = str(existing[0])
                    logger.info(f"[Persistence] Resolved order_no '{_raw_oid}' → UUID {order_id[:8]}...")
            except Exception:
                pass

        if not order_id:
            order_id = str(uuid.uuid4())
        order["order_id"] = order_id

        # ── 2b. BACKEND LOCK — reject saves on locked statuses ───────────
        # This enforces immutability at the service layer, not just UI.
        # Even if UI buttons are bypassed, save is blocked for locked orders.
        _LOCKED_STATUSES = {"BILLED", "DISPATCHED", "DELIVERED", "CLOSED"}
        if order_id:
            try:
                cur.execute(
                    "SELECT status FROM orders WHERE id = %s::uuid LIMIT 1",
                    (order_id,)
                )
                _db_row = cur.fetchone()
                if _db_row:
                    _db_status = str(_db_row[0] or "").upper()
                    if _db_status in _LOCKED_STATUSES:
                        raise PermissionError(
                            f"Order is locked (status: {_db_status}). "
                            f"No changes allowed after billing/dispatch."
                        )
            except PermissionError:
                raise
            except Exception as _lck_e:
                logger.warning(f"[Persistence] Lock check failed (non-critical): {_lck_e}")

        # ── 3. Collect lines & compute totals ──────────────────────────
        all_lines = []
        all_lines.extend(order.get("stock_lines", []))
        all_lines.extend(order.get("inhouse_lines", []))
        all_lines.extend(order.get("lab_order_lines", []))
        all_lines.extend(order.get("service_lines", []))  # consultation/eye-testing fees
        if not all_lines:                          # fallback to raw list
            all_lines = list(order.get("lines", []))

        total_items = len(all_lines)
        total_value = sum(
            float(l.get("billing_total") or l.get("total_price") or 0)
            for l in all_lines
        )

        # ── 4. Resolve operator ────────────────────────────────────────
        try:
            from modules.security.roles import current_user_name
            _operator = current_user_name()
        except Exception:
            import streamlit as _st
            _u = _st.session_state.get("user", "system")
            _operator = _u if isinstance(_u, str) else _u.get("name", "system")

        _is_new = not bool(order.get("_existed_in_db"))

        # ── 4b. Smart status advance ───────────────────────────────────
        # Status on first save from punching:
        #   PENDING / PENDING_VALIDATION / PROVISIONAL → UNDER_REVIEW
        # CONFIRMED is set only by backoffice SAVE TO ORDER.
        # FULL_ADVANCE parties: CONFIRMED → PENDING_PAYMENT until paid in full.
        _cur_st = order.get("status") or "PENDING"
        if _cur_st in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL"):
            order["status"] = "UNDER_REVIEW"
            logger.info(f"[Persistence] Auto-set {order.get('order_no')} → UNDER_REVIEW")

        # ── Payment gate for FULL_ADVANCE parties ───────────────────────
        if str(order.get("status") or "").upper() == "CONFIRMED":
            try:
                from modules.backoffice.order_status_live import apply_confirm_gate_to_persistence
                _oid = str(order.get("id") or order.get("order_id") or "")
                if _oid:
                    order = apply_confirm_gate_to_persistence(order)
                    if str(order.get("status","")).upper() == "PENDING_PAYMENT":
                        logger.info(
                            f"[Persistence] FULL_ADVANCE gate: "
                            f"{order.get('order_no')} → PENDING_PAYMENT "
                            f"(balance ₹{order.get('_gate_balance',0):,.2f})"
                        )
            except Exception as _ge:
                logger.warning(f"[Persistence] Confirm gate error: {_ge}")
        # ──────────────────────────────────────────────────────────────

        # ── 4c. Capture PREVIOUS status from DB before we overwrite it ─────────
        # We must read this BEFORE the upsert — once committed the DB has the
        # new status and we lose the "from" side of the transition.
        _prev_status_from_db = None
        try:
            cur.execute(
                "SELECT status FROM orders WHERE order_no = %s LIMIT 1",
                (order.get("order_no"),)
            )
            _row = cur.fetchone()
            if _row:
                _prev_status_from_db = _row[0]
        except Exception:
            pass
        # Fall back to in-memory hint or sensible default
        _prev_status = (
            _prev_status_from_db
            or order.get("_prev_status")
            or ("PENDING_VALIDATION" if _is_new else "PENDING")
        )
        # ───────────────────────────────────────────────────────────────────────

        # ── 5. UPSERT ORDER HEADER ─────────────────────────────────────
        #
        # Full path — uses the 4 extra columns once they are confirmed to exist.
        # Fallback   — only the 12 guaranteed-real columns; safe for any DB.
        #
        # ON CONFLICT (id): preserve created_at / created_by on updates;
        # only mutable fields are overwritten.
        # ──────────────────────────────────────────────────────────────
        if has_extra_order_cols:
            cur.execute("""
                INSERT INTO orders (
                    id, order_no, order_type, order_source,
                    party_id,
                    status, total_items, total_value, created_at,
                    party_name, patient_name, patient_mobile, customer_order_no,
                    created_by, updated_at, updated_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (id) DO UPDATE SET
                    status            = EXCLUDED.status,
                    total_items       = EXCLUDED.total_items,
                    total_value       = EXCLUDED.total_value,
                    party_name        = EXCLUDED.party_name,
                    party_id          = EXCLUDED.party_id,
                    patient_name      = EXCLUDED.patient_name,
                    patient_mobile    = EXCLUDED.patient_mobile,
                    customer_order_no = EXCLUDED.customer_order_no,
                    order_type        = EXCLUDED.order_type,
                    order_source      = EXCLUDED.order_source,
                    updated_at        = NOW(),
                    updated_by        = EXCLUDED.updated_by
            """, (
                order_id,
                order.get("order_no"),
                order.get("order_type"),
                order.get("order_source", "unknown"),
                order.get("party_id") or None,
                order.get("status", "PENDING"),
                total_items,
                total_value,
                order.get("created_at") or datetime.datetime.now(),
                order.get("party_name"),
                order.get("patient_name"),
                order.get("patient_mobile"),
                order.get("customer_order_no"),
                _operator,
                _operator,
            ))
        else:
            # Safe fallback — only confirmed-real columns
            cur.execute("""
                INSERT INTO orders (
                    id, order_no, order_type, order_source,
                    status, total_items, total_value, created_at,
                    party_name, patient_name, patient_mobile, customer_order_no
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status            = EXCLUDED.status,
                    total_items       = EXCLUDED.total_items,
                    total_value       = EXCLUDED.total_value,
                    party_name        = EXCLUDED.party_name,
                    patient_name      = EXCLUDED.patient_name,
                    patient_mobile    = EXCLUDED.patient_mobile,
                    customer_order_no = EXCLUDED.customer_order_no,
                    order_type        = EXCLUDED.order_type,
                    order_source      = EXCLUDED.order_source
            """, (
                order_id,
                order.get("order_no"),
                order.get("order_type"),
                order.get("order_source", "unknown"),
                order.get("status", "PENDING"),
                total_items,
                total_value,
                order.get("created_at") or datetime.datetime.now(),
                order.get("party_name"),
                order.get("patient_name"),
                order.get("patient_mobile"),
                order.get("customer_order_no"),
            ))

        # ── 6. SOFT-DELETE OLD LINES (hard DELETE blocked by DB trigger) ──
        cur.execute(
            "UPDATE order_lines SET is_deleted=TRUE, deleted_at=NOW() WHERE order_id = %s AND COALESCE(is_deleted,FALSE)=FALSE",
            (order_id,)
        )

        # ── 7. UPSERT FRESH LINES ──────────────────────────────────────
        # ON CONFLICT: if line id already exists (e.g. re-save of same line),
        # update it in place rather than insert a duplicate.
        # ──────────────────────────────────────────────────────────────
        if has_line_pricing:
            insert_sql = """
                INSERT INTO order_lines (
                    id, order_id, product_id,
                    sph, cyl, axis, add_power, eye_side,
                    quantity, unit_price, total_price,
                    gst_percent, gst_amount,
                    discount_percent, discount_amount,
                    status, lens_params, boxing_params, allocated_qty
                )
                VALUES (%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    quantity=EXCLUDED.quantity, unit_price=EXCLUDED.unit_price,
                    total_price=EXCLUDED.total_price, gst_percent=EXCLUDED.gst_percent,
                    gst_amount=EXCLUDED.gst_amount, discount_percent=EXCLUDED.discount_percent,
                    discount_amount=EXCLUDED.discount_amount, status=EXCLUDED.status,
                    lens_params=EXCLUDED.lens_params, boxing_params=EXCLUDED.boxing_params,
                    allocated_qty=EXCLUDED.allocated_qty, is_deleted=FALSE, deleted_at=NULL
            """
        else:
            insert_sql = """
                INSERT INTO order_lines (
                    id, order_id, product_id,
                    sph, cyl, axis, add_power, eye_side,
                    quantity, unit_price, total_price,
                    status, lens_params, boxing_params, allocated_qty
                )
                VALUES (%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    quantity=EXCLUDED.quantity, unit_price=EXCLUDED.unit_price,
                    total_price=EXCLUDED.total_price, status=EXCLUDED.status,
                    lens_params=EXCLUDED.lens_params, boxing_params=EXCLUDED.boxing_params,
                    allocated_qty=EXCLUDED.allocated_qty, is_deleted=FALSE, deleted_at=NULL
            """

        # ── Blank-lock guard: block RX changes on allocated lines ──────────
        # If a line has a blank_allocations row, sph/cyl/axis cannot be changed.
        # Any attempt to save with changed RX raises an error — user must
        # reset/cancel the job card first (from Documents → Job Cards tab).
        import math as _mth

        def _rx_equal(a, b):
            try:
                fa, fb = float(a or 0), float(b or 0)
                if _mth.isnan(fa): fa = 0.0
                if _mth.isnan(fb): fb = 0.0
                return abs(fa - fb) < 0.02
            except: return True

        _lock_errors = []
        try:
            from modules.sql_adapter import run_query as _rqpe
            import json as _jpe
            for _ll in all_lines:
                _lid = (_ll.get("line_id") or _ll.get("id") or "").strip()
                if not _lid: continue
                _alloc = _rqpe(
                    "SELECT ba.id FROM blank_allocations ba WHERE ba.order_line_id=%(l)s::uuid LIMIT 1",
                    {"l": _lid}
                )
                if not _alloc:
                    continue
                # Line is locked — check if RX changed vs DB
                _db_row = _rqpe(
                    "SELECT sph, cyl, axis FROM order_lines WHERE id=%(l)s::uuid LIMIT 1",
                    {"l": _lid}
                )
                if not _db_row:
                    continue
                _db = _db_row[0]
                if (not _rx_equal(_ll.get("sph"), _db.get("sph")) or
                    not _rx_equal(_ll.get("cyl"), _db.get("cyl")) or
                    not _rx_equal(_ll.get("axis"), _db.get("axis"))):
                    eye = (_ll.get("eye_side") or "?").upper()[:1]
                    _lock_errors.append(
                        f"{eye} Eye ({_ll.get('product_name','?')}): "
                        f"RX changed but blank is allocated. "
                        f"Reset or cancel the job card first."
                    )
        except Exception:
            pass  # guard is best-effort — never block save due to lock-check error

        if _lock_errors:
            import streamlit as _stpe
            for _le in _lock_errors:
                _stpe.error(f"🔒 Locked: {_le}")
            conn.rollback()
            return
        # ─────────────────────────────────────────────────────────────────────

        def _safe_numeric(v):
            """Return float or None — coerces NaN/Inf/empty/non-numeric to None."""
            if v is None:
                return None
            try:
                f = float(v)
                return None if (_mth.isnan(f) or _mth.isinf(f)) else f
            except (TypeError, ValueError):
                return None

        def _safe_axis(v):
            """Return int or None — axis is an INTEGER column; float 180.0 → 180."""
            if v is None:
                return None
            try:
                f = float(v)
                if _mth.isnan(f) or _mth.isinf(f):
                    return None
                i = int(round(f))
                # SMALLINT range guard: -32768..32767 (axis is 0-180, but be safe)
                if i < -32768 or i > 32767:
                    return None
                return i
            except (TypeError, ValueError):
                return None

        for line in all_lines:
            # Persist manufacturing_route inside lens_params so it round-trips
            lens_params = dict(line.get("lens_params") or {})
            if line.get("manufacturing_route"):
                lens_params["manufacturing_route"] = line["manufacturing_route"]
            # Persist batch_no (frame SKU) so order_loader can rebuild display name
            if line.get("batch_no"):
                lens_params["batch_no"] = line["batch_no"]
            # Persist frame attributes for the description column in backoffice
            if line.get("frame_group"):
                lens_params["frame_group"] = line["frame_group"]
            if line.get("colour_mix"):
                lens_params["colour_mix"] = line["colour_mix"]
            # Persist display product_name so backoffice always shows SKU+group+colour
            if line.get("product_name"):
                lens_params["display_product_name"] = line["product_name"]
            # Persist batch_allocation so allocation survives save/reload
            _ba_save = line.get("batch_allocation") or []
            if _ba_save:
                lens_params["batch_allocation"] = _ba_save
                lens_params["batch_status"]     = line.get("batch_status", "ALLOCATED")
            elif "batch_allocation" not in lens_params:
                lens_params["batch_allocation"] = []

            quantity    = int(line.get("billing_qty")    or line.get("quantity")    or 0)
            total_price = float(line.get("billing_total") or line.get("total_price") or 0)

            # ── CRITICAL: reuse existing line id so ON CONFLICT (id) fires ──
            # str(uuid.uuid4()) was here before — generated a NEW uuid on every
            # save, meaning ON CONFLICT never matched and a duplicate row was
            # inserted every time the order was saved. Use line_id from the
            # loaded dict; only generate a new uuid for truly new lines.
            line_id = (
                line.get("line_id")
                or line.get("id")
                or str(uuid.uuid4())
            )

            base = (
                line_id,
                order_id,
                line.get("product_id"),
                _safe_numeric(line.get("sph")),
                _safe_numeric(line.get("cyl")),
                _safe_axis(line.get("axis")),       # INTEGER column — must be int/None
                _safe_numeric(line.get("add_power")),
                line.get("eye_side"),
                quantity,
                float(line.get("unit_price") or 0),
                total_price,
            )

            if has_line_pricing:
                pricing = (
                    float(line.get("gst_percent")      or 0),
                    float(line.get("gst_amount")       or 0),
                    float(line.get("discount_percent") or 0),
                    float(line.get("discount_amount")  or 0),
                )
                tail = (
                    line.get("status", "PENDING"),
                    json.dumps(lens_params),
                    json.dumps(line.get("boxing_params") or {}),
                    int(line.get("allocated_qty") or 0),
                )
                cur.execute(insert_sql, base + pricing + tail)
            else:
                tail = (
                    line.get("status", "PENDING"),
                    json.dumps(lens_params),
                    json.dumps(line.get("boxing_params") or {}),
                    int(line.get("allocated_qty") or 0),
                )
                cur.execute(insert_sql, base + tail)

        # ── 8. COMMIT ─────────────────────────────────────────────────
        conn.commit()

        _action = "ORDER_CREATED" if _is_new else "ORDER_UPDATED"
        _audit_order(_action, order_id, order.get("order_no", ""), {
            "order_type":  order.get("order_type"),
            "total_value": total_value,
            "total_items": total_items,
            "operator":    _operator,
        })

        # ── Patch in-memory session state so UI shows new status immediately ─
        try:
            import streamlit as _st
            _ono    = order.get("order_no")
            _st_now = order.get("status", "PENDING")
            for _cached_order in _st.session_state.get("bo_active_orders", []):
                if _cached_order.get("order_no") == _ono:
                    _cached_order["status"]     = _st_now
                    _cached_order["updated_at"] = str(datetime.datetime.now())[:16]
                    break
        except Exception:
            pass

        # ── Log status transition to audit trail ──────────────────────────────
        try:
            from modules.backoffice.event_logger import log_event, EventType
            import json as _json
            _now_status = order.get("status", "PENDING")

            if _is_new:
                # New order: log ORDER_SAVED (received) event
                log_event(
                    EventType.ORDER_SAVED,
                    order_id,
                    details={
                        "from_status": None,
                        "to_status":   _now_status,
                        "order_no":    order.get("order_no"),
                        "action":      "ORDER_CREATED",
                    },
                    source=_operator or "system",
                    remarks=f"Order created → {_now_status}",
                )
            # Log STATUS_CHANGED for every save (new or update) with clean from/to
            if _prev_status != _now_status or _is_new:
                log_event(
                    EventType.STATUS_CHANGED,
                    order_id,
                    details={
                        "from_status": _prev_status,
                        "to_status":   _now_status,
                        "order_no":    order.get("order_no"),
                        "action":      "ORDER_SAVED" if _is_new else "ORDER_UPDATED",
                    },
                    source=_operator or "system",
                    remarks=f"{'Created' if _is_new else 'Saved'}: {_prev_status} → {_now_status}",
                )
            # Write status history ONLY when status actually changed or order is new
            if _prev_status != _now_status or _is_new:
                from modules.sql_adapter import run_query as _rq
                _rq("""
                    INSERT INTO order_status_history
                        (history_id, order_id, from_status, to_status,
                         changed_at, changed_by_name, remarks)
                    SELECT gen_random_uuid()::uuid, id, %(frm)s, %(to)s,
                           NOW(), %(by)s, %(rmk)s
                    FROM orders WHERE order_no = %(ono)s
                    ON CONFLICT DO NOTHING
                """, {
                    "frm": _prev_status,
                    "to":  _now_status,
                    "by":  _operator or "system",
                    "rmk": f"{'Created' if _is_new else 'Saved'} → {_now_status}",
                    "ono": order.get("order_no"),
                })
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────────

        return order_id

    except Exception as e:
        conn.rollback()
        try:
            from modules.core.error_logger import log_error
            log_error(e, context="order_persistence.save_order_to_db",
                      payload={"order_no": order.get("order_no"),
                               "order_id": order.get("order_id")})
        except Exception:
            pass
        raise e

    finally:
        cur.close()
        conn.close()
