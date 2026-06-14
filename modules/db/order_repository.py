import psycopg2
import psycopg2.extras
from typing import List, Dict, Optional
from decimal import Decimal
import numpy as np
import logging

from modules.sql_adapter import get_transaction_connection, close_connection
from modules.core.json_sanitizer import sanitize_json

logger = logging.getLogger(__name__)


# ============================================================================
# VALUE NORMALIZER (Decimal + numpy safe)
# ============================================================================

def normalize_value(v):
    """
    Convert Decimal / numpy / pandas types to native Python
    """

    # Decimal → float
    if isinstance(v, Decimal):
        return float(v)

    # numpy int
    if isinstance(v, (np.integer,)):
        return int(v)

    # numpy float
    if isinstance(v, (np.floating,)):
        return float(v)

    return v


def _round_order_header_total(order_data: Dict) -> None:
    """Round customer-facing retail order total without changing line GST values."""
    try:
        if str(order_data.get("order_type") or "").upper() != "RETAIL":
            return
        order_data["total_value"] = float(round(float(order_data.get("total_value") or 0)))
    except Exception:
        return


# ============================================================================
# MAIN SAVE FUNCTION
# ============================================================================

# Cache: True = pricing columns confirmed to exist, False = use fallback INSERT
_PRICING_COLS_EXIST: bool | None = None



def _ensure_display_order_no(cursor) -> bool:
    """
    Ensure orders table has a display_order_no column.
    Uses the transactional order_number_registry (no PostgreSQL SEQUENCE).
    Safe to call on every save — idempotent.
    """
    try:
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='orders' AND column_name='display_order_no'
        """)
        if not cursor.fetchone():
            cursor.execute("""
                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS display_order_no INTEGER
            """)
        # Ensure registry table exists (no-op if already there)
        from modules.db.order_number_registry import ensure_registry
        ensure_registry(cursor)
        return True
    except Exception as _e:
        import logging
        logging.warning(f"[OrderRepo] display_order_no migration failed (non-fatal): {_e}")
        return False


def _ensure_pricing_columns(cursor) -> bool:
    """
    Check if the pricing columns exist on order_lines.
    Auto-creates them if missing (idempotent DDL).
    Caches the result so we only query information_schema once per process.
    """
    global _PRICING_COLS_EXIST
    if _PRICING_COLS_EXIST is not None:
        return _PRICING_COLS_EXIST

    try:
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'order_lines'
              AND column_name = 'gst_amount'
        """)
        exists = cursor.fetchone()[0] > 0
        if not exists:
            logger.info("Pricing columns missing from order_lines — adding them now")
            cursor.execute("""
                ALTER TABLE order_lines
                    ADD COLUMN IF NOT EXISTS gst_percent    NUMERIC(5,2)  DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS gst_amount     NUMERIC(12,2) DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS discount_percent NUMERIC(5,2) DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS discount_amount  NUMERIC(12,2) DEFAULT 0
            """)
            logger.info("Pricing columns added to order_lines")
        # Idempotent: add audit/net columns even when gst_amount already exists.
        # applied_rule_ids is TEXT because the active discount engine stamps a
        # comma-separated rule id string; changing it to JSONB would break reads.
        cursor.execute("""
            ALTER TABLE order_lines
                ADD COLUMN IF NOT EXISTS billing_total NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS discount_rule TEXT DEFAULT '',
                ADD COLUMN IF NOT EXISTS applied_rule_ids TEXT DEFAULT ''
        """)
        _PRICING_COLS_EXIST = True
        return True
    except Exception as e:
        logger.warning(f"Could not verify/create pricing columns: {e}")
        _PRICING_COLS_EXIST = False
        return False


_SERVICE_COLS_EXIST: Optional[bool] = None

def _ensure_service_columns(cursor) -> bool:
    """
    Ensure is_service_line and allocated_qty exist on order_lines.
    SERVICE lines (consultation fee) need both:
      - is_service_line = TRUE  so billing knows to skip stock allocation
      - allocated_qty   = qty   so billing gate considers them ready immediately
    Idempotent — safe to call on every save.
    """
    global _SERVICE_COLS_EXIST
    if _SERVICE_COLS_EXIST is not None:
        return _SERVICE_COLS_EXIST
    try:
        cursor.execute("""
            ALTER TABLE order_lines
                ADD COLUMN IF NOT EXISTS is_service_line BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS allocated_qty   INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS ready_qty       INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS billed_qty      INTEGER DEFAULT 0
        """)
        _SERVICE_COLS_EXIST = True
        return True
    except Exception as _e:
        import logging
        logging.warning(f"[OrderRepo] service columns migration failed (non-fatal): {_e}")
        _SERVICE_COLS_EXIST = False
        return False


_EXTRA_DATA_COL_EXIST: Optional[bool] = None

def _ensure_extra_data_column(cursor) -> bool:
    """
    Ensure orders.extra_data JSONB column exists.
    Used to store wholesale end-customer details without polluting
    structured columns (patient_name, patient_mobile).
    """
    global _EXTRA_DATA_COL_EXIST
    if _EXTRA_DATA_COL_EXIST is not None:
        return _EXTRA_DATA_COL_EXIST
    try:
        cursor.execute("SAVEPOINT _ensure_extra")
        cursor.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS extra_data JSONB DEFAULT '{}'::jsonb
        """)
        cursor.execute("RELEASE SAVEPOINT _ensure_extra")
        _EXTRA_DATA_COL_EXIST = True
        return True
    except Exception as _e:
        import logging
        logging.warning(f"[OrderRepo] extra_data column migration failed: {_e}")
        try: cursor.execute("ROLLBACK TO SAVEPOINT _ensure_extra")
        except Exception: pass
        _EXTRA_DATA_COL_EXIST = False
        return False


def save_order(order_data: Dict, lines: List[Dict], user_name: str):
    """
    Saves order + lines + status history in ONE transaction.
    Fully JSON + Decimal safe.
    Auto-adds pricing columns (gst_amount, discount_*) if missing from DB.
    Auto-adds is_service_line / allocated_qty for SERVICE lines.
    """

    conn = None
    cursor = None

    try:
        _round_order_header_total(order_data)

        conn = get_transaction_connection()
        cursor = conn.cursor()

        # ============================================================
        # ENSURE TABLE STRUCTURE EXISTS FIRST
        # ============================================================
        # Check/auto-create pricing columns — safe if migration not yet run
        has_pricing_cols    = _ensure_pricing_columns(cursor)
        # Ensure is_service_line / allocated_qty columns exist
        has_service_cols    = _ensure_service_columns(cursor)
        # Ensure sequential display number column exists
        has_display_no      = _ensure_display_order_no(cursor)
        # Ensure extra_data JSONB column exists (end-customer details etc.)
        has_extra_data      = _ensure_extra_data_column(cursor)

        # ============================================================
        # INSERT ORDER HEADER
        # ============================================================
        # Use different SQL based on whether display_order_no exists
        # ── Claim next sequential number (transactional, gap-free) ──────────
        # This locks order_number_registry FOR UPDATE — concurrent saves
        # on any server block here until this transaction commits.
        # If this transaction rolls back, the number is never consumed.
        _display_no = 0
        if has_display_no:
            try:
                from modules.db.order_number_registry import next_order_number, format_doc_number
                _, _display_no = next_order_number(
                    cursor,
                    order_type=order_data.get("order_type", "RETAIL"),
                )
                # ── FIX: Build formatted order_no HERE, inside this transaction.
                # Previously this was done in order_pipeline.py via a SECOND run_write()
                # after this commit — which caused gaps when that second write failed.
                # Now order_no is set correctly before the single commit below.
                if _display_no > 0:
                    _otype = order_data.get("order_type", "RETAIL").upper()
                    order_data["order_no"] = format_doc_number(_otype, _display_no)
            except Exception as _seq_e:
                logger.error("[OrderRepo] number registry failed before order insert", exc_info=True)
                raise RuntimeError(f"Order number allocation failed before order insert: {_seq_e}") from _seq_e

        order_sql = """
            INSERT INTO orders (
                id, order_no, order_type, order_source, status,
                party_name, patient_name, patient_mobile, customer_order_no,
                total_items, total_value, party_id, payment_mode, created_at,
                display_order_no
            )
            VALUES (
                gen_random_uuid(), %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s::uuid, %s, NOW(),
                %s
            )
            RETURNING id;
        """

        cursor.execute(order_sql, (
            order_data["order_no"],
            order_data["order_type"],
            order_data.get("order_source", "unknown"),
            order_data["status"],
            order_data.get("party_name"),
            order_data.get("patient_name"),
            order_data.get("patient_mobile"),
            order_data.get("customer_order_no"),
            normalize_value(order_data["total_items"]),
            normalize_value(order_data["total_value"]),
            order_data.get("party_id") or None,
            order_data.get("payment_mode") or "ON_COMPLETION",
            _display_no if _display_no > 0 else None,
        ))

        _row = cursor.fetchone()
        order_id = _row[0]

        # ── Write extra_data JSONB (end-customer/wholesale ref) ──────────
        if has_extra_data:
            _extra = order_data.get("extra_data") or {}
            if _extra:
                try:
                    cursor.execute(
                        "UPDATE orders SET extra_data = %s::jsonb WHERE id = %s",
                        (psycopg2.extras.Json(sanitize_json(_extra)), str(order_id))
                    )
                except Exception as _ed_e:
                    import logging as _edl
                    _edl.warning(f"[OrderRepo] extra_data write failed (non-fatal): {_ed_e}")

        # Build line INSERT SQL — include service columns when available
        _svc_col_clause = ", is_service_line, allocated_qty, ready_qty, status" if has_service_cols else ", status"
        _svc_val_clause = ", %s, %s, %s, %s"                                    if has_service_cols else ", %s"

        if has_pricing_cols:
            line_sql = f"""
            INSERT INTO order_lines (
                id, order_id, product_id,
                sph, cyl, axis, add_power, eye_side,
                quantity, unit_price, total_price,
                gst_percent, gst_amount,
                discount_percent, discount_amount,
                billing_total, discount_rule, applied_rule_ids,
                lens_params, boxing_params, suggested_allocation
                {_svc_col_clause}
            )
            VALUES (
                gen_random_uuid(), %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s
                {_svc_val_clause}
            )
            RETURNING id;
            """
        else:
            # Fallback: original columns only (no pricing columns)
            line_sql = f"""
            INSERT INTO order_lines (
                id, order_id, product_id,
                sph, cyl, axis, add_power, eye_side,
                quantity, unit_price, total_price,
                lens_params, boxing_params, suggested_allocation
                {_svc_col_clause}
            )
            VALUES (
                gen_random_uuid(), %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
                {_svc_val_clause}
            )
            RETURNING id;
            """

        # Dedup guard: track (product_id, eye_side) already inserted this call
        _inserted_keys = set()

        # ── Apply discount rules ONCE before the line loop ───────────────────
        # Shared helper also restamps total_price/billing_total and GST on the
        # net amount, so discount carries from punching through billing.
        try:
            from modules.pricing.discount_flow import apply_order_discounts
            _order_type_disc = str(order_data.get("order_type") or "wholesale")

            # party_id may be missing from order_data (wholesale flow only passes party_name).
            # Resolve UUID from parties table using party_name as fallback.
            _party_id_disc = str(order_data.get("party_id") or "").strip()
            if not _party_id_disc:
                _party_name_disc = str(
                    order_data.get("party_name") or
                    order_data.get("party") or ""
                ).strip()
                if _party_name_disc:
                    try:
                        _pid_rows = cursor.execute(
                            "SELECT id::text AS id FROM parties "
                            "WHERE party_name = %s AND COALESCE(is_active,TRUE)=TRUE "
                            "LIMIT 1",
                            (_party_name_disc,)
                        ) or cursor.fetchone()
                        if _pid_rows:
                            _party_id_disc = str(_pid_rows[0] if isinstance(_pid_rows, tuple)
                                                 else _pid_rows.get("id", ""))
                    except Exception:
                        pass

            apply_order_discounts(lines, party_id=_party_id_disc, order_type=_order_type_disc)
            try:
                from modules.pricing.supplier_scheme_engine import apply_customer_scheme_to_line
                for _idx, _line in enumerate(lines):
                    lines[_idx] = apply_customer_scheme_to_line(
                        _line, party_id=_party_id_disc, order_type=_order_type_disc
                    )
            except Exception as _scheme_e:
                logger.debug("[OrderRepo] supplier scheme skipped: %s", _scheme_e)
            try:
                from modules.pricing.cart_scheme_engine import apply_cart_schemes
                lines, _cart_result = apply_cart_schemes(
                    lines, party_id=_party_id_disc, order_type=_order_type_disc
                )
                if getattr(_cart_result, "applied", False):
                    logger.info("[OrderRepo] cart scheme applied: %s", getattr(_cart_result, "message", ""))
            except Exception as _cart_e:
                logger.debug("[OrderRepo] cart scheme skipped: %s", _cart_e)
            try:
                from modules.pricing.tax_engine import apply_taxes
                apply_taxes({
                    "order_type": _order_type_disc,
                    "lines": lines,
                    "net_value": sum(float(_l.get("billing_total") or _l.get("total_price") or 0) for _l in lines),
                })
            except Exception as _tax_e:
                logger.debug("[OrderRepo] post-scheme tax restamp skipped: %s", _tax_e)

            # Header total must follow the final line values after discounts,
            # supplier schemes, cart/free offers, and tax restamping. The order
            # row is already inserted in this transaction, so correct it before
            # the same commit; otherwise reports/payment gates can read a stale
            # punched total while order_lines carry the scheme-adjusted values.
            _line_net_total = round(sum(
                float(_l.get("billing_total") or _l.get("total_price") or 0)
                for _l in lines
            ), 2)
            if str(_order_type_disc or "").upper() == "RETAIL":
                _line_net_total = float(round(_line_net_total))
            order_data["total_value"] = _line_net_total
            cursor.execute(
                "UPDATE orders SET total_value = %s WHERE id = %s",
                (normalize_value(_line_net_total), order_id),
            )
        except Exception as _disc_e:
            logger.warning(f"[OrderRepo] discount_engine skipped: {_disc_e}")

        for line in lines:
            # Skip duplicate (product_id, eye_side) within same order save
            _dup_key = (
                str(line.get("product_id","") or ""),
                str(line.get("eye_side","") or "").upper().strip()[:1],
            )
            if _dup_key in _inserted_keys:
                logger.warning(f"[order_repository] Skipping duplicate line: product={_dup_key[0][:8]} eye={_dup_key[1]}")
                continue
            _inserted_keys.add(_dup_key)

            # eye_side column is char(1) — map multi-char values to single char
            _eye_side_raw = str(line.get("eye_side") or "").upper().strip()
            # Expand any already-expanded values first, then compact for DB storage
            _eye_expand_pre = {"OTHER":"O","SERVICE":"S","R":"R","L":"L","B":"B"}
            _eye_side_pre = _eye_expand_pre.get(_eye_side_raw, _eye_side_raw[:1] or "O")
            _eye_side_map = {"R": "R", "L": "L", "B": "B", "O": "O", "S": "S"}
            _eye_side = _eye_side_map.get(_eye_side_pre, _eye_side_pre[:1] or "O")

            # SERVICE lines: courier/other bill directly; colouring/fitting are
            # production services and must stay pending until their service job
            # reaches billing readiness.
            _is_svc = (_eye_side == "S") or bool(line.get("is_service_line"))
            _lp_for_service = line.get("lens_params") if isinstance(line.get("lens_params"), dict) else {}
            _svc_prod_route = str(_lp_for_service.get("service_production_type") or "").upper().strip()

            # Use billing_qty (pcs count from allocation) with fallback to quantity.
            # Product rows keep total_price and billing_total aligned as taxable/net.
            # Wholesale service rows may carry billing_total as GST-inclusive payable,
            # so preserve the source total_price instead of copying billing_total over it.
            _quantity = int(line.get("billing_qty") or line.get("quantity") or 0)
            _billing_total = float(line.get("billing_total") or line.get("total_price") or 0)
            _total_price = float(
                line.get("total_price")
                if line.get("total_price") is not None
                else _billing_total
            )

            base_params = (
                order_id,
                line.get("product_id"),
                normalize_value(line.get("sph")),
                normalize_value(line.get("cyl")),
                normalize_value(line.get("axis")),
                normalize_value(line.get("add_power")),
                _eye_side,
                _quantity,
                normalize_value(line.get("unit_price")),
                _total_price,
            )
            if has_pricing_cols:
                pricing_params = (
                    normalize_value(line.get("gst_percent", 0)),
                    normalize_value(line.get("gst_amount", 0)),
                    normalize_value(line.get("discount_percent", 0)),
                    normalize_value(line.get("discount_amount", 0)),
                    normalize_value(_billing_total),
                    str(line.get("discount_rule") or ""),
                    str(line.get("applied_rule_ids") or ""),
                )
            else:
                pricing_params = ()

            json_params = (
                psycopg2.extras.Json(sanitize_json({
                    **line.get("lens_params", {}),
                    # Fix 8: persist SKU+colour so backoffice always shows full name
                    **({"display_product_name": line["product_name"]}
                       if line.get("product_name") else {}),
                    **({"colour_mix": line["colour_mix"]}
                       if line.get("colour_mix") else {}),
                    **({"frame_group": line["frame_group"]}
                       if line.get("frame_group") else {}),
                    **({"batch_no": line["batch_no"]}
                       if line.get("batch_no") else {}),
                })),
                psycopg2.extras.Json(sanitize_json(line.get("boxing_params", {}))),
                psycopg2.extras.Json(sanitize_json(line.get("suggested_allocation", []))),
            )

            if has_service_cols:
                # is_service_line, allocated_qty, ready_qty, status
                svc_params = (
                    _is_svc,
                    (0 if _svc_prod_route else _quantity) if _is_svc else int(line.get("allocated_qty") or 0),
                    (0 if _svc_prod_route else _quantity) if _is_svc else int(line.get("ready_qty") or 0),
                    ("PENDING" if _svc_prod_route else "READY") if _is_svc else "PENDING",
                )
            else:
                svc_params = ("READY" if _is_svc else "PENDING",)

            cursor.execute(line_sql, base_params + pricing_params + json_params + svc_params)
            _line_row = cursor.fetchone()
            _saved_line_id = _line_row[0] if _line_row else None

            # Stock rows are "seat booked" at order save. The line may be a
            # frame SKU, contact-lens batch, or ophthalmic stock batch; in all
            # cases the selected inventory row must be blocked immediately so
            # the next order sees reduced availability.
            try:
                _lp_stock = line.get("lens_params") if isinstance(line.get("lens_params"), dict) else {}
                _route_stock = str(
                    line.get("manufacturing_route") or _lp_stock.get("manufacturing_route") or ""
                ).upper()
                _alloc_qty_total = int(line.get("allocated_qty") or 0)
                if _route_stock == "STOCK" and _alloc_qty_total > 0 and _saved_line_id:
                    _alloc_rows = (
                        line.get("batch_allocation")
                        or _lp_stock.get("batch_allocation")
                        or line.get("suggested_allocation")
                        or []
                    )
                    if isinstance(_alloc_rows, dict):
                        _alloc_rows = [_alloc_rows]
                    if not _alloc_rows:
                        _stock_id = str(_lp_stock.get("stock_id") or _lp_stock.get("batch_id") or "").strip()
                        _batch_no = str(_lp_stock.get("batch_no") or line.get("batch_no") or "").strip()
                        _alloc_rows = [{
                            "stock_id": _stock_id,
                            "batch_id": _stock_id,
                            "batch_no": _batch_no,
                            "allocated_qty": _alloc_qty_total,
                        }]

                    _pid_stock = str(line.get("product_id") or "").strip()
                    _reserved_total = 0
                    for _ar in _alloc_rows:
                        if not isinstance(_ar, dict):
                            continue
                        _ar_qty = int(_ar.get("allocated_qty") or _ar.get("qty") or 0)
                        if _ar_qty <= 0:
                            continue
                        _ar_sid = str(_ar.get("stock_id") or _ar.get("batch_id") or "").strip()
                        _ar_bno = str(_ar.get("batch_no") or "").strip()
                        if not _ar_sid and (not _pid_stock or not _ar_bno):
                            continue
                        if _ar_sid:
                            cursor.execute(
                                """
                                UPDATE inventory_stock
                                   SET allocated_qty = COALESCE(allocated_qty, 0) + %s,
                                       updated_at = NOW()
                                 WHERE id = %s::uuid
                                   AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) >= %s
                                """,
                                (_ar_qty, _ar_sid, _ar_qty),
                            )
                        else:
                            cursor.execute(
                                """
                                UPDATE inventory_stock
                                   SET allocated_qty = COALESCE(allocated_qty, 0) + %s,
                                       updated_at = NOW()
                                 WHERE product_id = %s::uuid
                                   AND UPPER(TRIM(batch_no)) = UPPER(TRIM(%s))
                                   AND GREATEST(0, COALESCE(quantity,0) - COALESCE(allocated_qty,0)) >= %s
                                """,
                                (_ar_qty, _pid_stock, _ar_bno, _ar_qty),
                            )
                        if cursor.rowcount != 1:
                            raise ValueError("Selected stock row is no longer available")
                        _reserved_total += _ar_qty

                    if _reserved_total != _alloc_qty_total:
                        raise ValueError("Stock allocation quantity mismatch during order save")
            except Exception:
                raise

        # ============================================================
        # INSERT STATUS HISTORY
        # ============================================================
        history_sql = """
        INSERT INTO order_status_history (
            history_id, order_id,
            from_status, to_status,
            changed_by_name, remarks
        )
        VALUES (
            gen_random_uuid(), %s,
            NULL, %s,
            %s, %s
        );
        """

        cursor.execute(history_sql, (
            order_id,
            order_data["status"],
            user_name,
            "Order Created"
        ))

        # ============================================================
        # COMMIT
        # ============================================================
        conn.commit()

        logger.info(f"Order saved successfully: {order_id}")
        return {
            "order_db_id":      str(order_id),
            "display_order_no": _display_no,
            "order_no":         order_data["order_no"],  # formatted inside same commit, no second write needed
        }

    except Exception as e:
        if conn:
            conn.rollback()

        logger.error("Order save failed", exc_info=True)
        raise e

    finally:
        if cursor:
            cursor.close()

        if conn:
            close_connection(conn)


# ============================================================================
# FETCH BACKOFFICE ORDERS
# ============================================================================

def fetch_backoffice_orders(limit=None):
    """
    Fetch orders for backoffice display.

    Args:
        limit (int, optional): Max rows to return. None = all rows.

    Returns:
        list[dict]: Orders sorted newest-first.
    """

    conn = None
    cursor = None

    try:
        conn = get_transaction_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        query = """
            SELECT
                id,
                order_no,
                order_type,
                COALESCE(order_source, order_type) AS order_source,
                status,
                party_name,
                patient_name,
                patient_mobile,
                customer_order_no,
                total_items,
                total_value,
                created_at
            FROM orders
            WHERE COALESCE(is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(order_type, 'RETAIL')) != 'CONSULTATION'
            ORDER BY created_at DESC
        """

        if limit:
            query += f" LIMIT {int(limit)}"

        cursor.execute(query)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    except Exception as e:
        logger.error("fetch_backoffice_orders failed", exc_info=True)
        raise e

    finally:
        if cursor:
            cursor.close()
        if conn:
            close_connection(conn)
