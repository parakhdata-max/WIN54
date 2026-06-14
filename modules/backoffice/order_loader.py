from modules.sql_adapter import get_connection

def _parse_jsonb(val):
    """Safely parse a JSONB value that may come as str, dict, or None."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        import json as _json
        try:
            return _json.loads(val) or {}
        except Exception:
            return {}
    return {}



import streamlit as st


@st.cache_data(ttl=30, show_spinner=False)
def load_orders_summary(limit: int = 100, include_closed: bool = False) -> list:
    """Fast order list — headers only, no line detail. Used by backoffice list view.

    Adds two roll-up fields per order so cards can show the right number even
    when older retail orders saved order_lines.total_price as the gross
    (pre-discount) value:

      total_discount  — SUM(order_lines.discount_amount)
      net_total_value — total_value adjusted for discount when total_value
                        looks like the gross sum (sum of unit_price*qty)

    The heuristic compares orders.total_value against
    sum(unit_price * quantity) — the TRUE gross — rather than against
    sum(total_price). This way the heuristic stays correct after retail
    punching is fixed to write net into total_price (otherwise total_value
    and sum(total_price) become equal for both gross-era and net-era rows
    and the comparison loses signal).
    """
    from modules.core.system_observer import perf_step
    conn = get_connection()
    cur  = conn.cursor()
    try:
        with perf_step(f"Backoffice summary list ({limit})", category="loader", detail=f"include_closed={include_closed}"):
            closed_filter = ""
            if not include_closed:
                closed_filter = """
                    AND UPPER(COALESCE(o.status, '')) NOT IN (
                        'DELIVERED', 'CLOSED', 'CANCELLED', 'RETURNED'
                    )
                """

            # Try with discount_amount aggregation; fall back if column not yet added
            try:
                cur.execute(f"""
                SELECT
                    o.id::text AS order_id,
                    o.id::text AS id,
                    o.order_no,
                    COALESCE(o.patient_name, o.party_name, '\u2014') AS patient_name,
                    COALESCE(o.party_name, '')     AS party_name,
                    COALESCE(o.status, 'PENDING') AS status,
                    o.created_at,
                    COALESCE(o.total_value, 0)    AS total_value,
                    COALESCE(o.order_type, 'RETAIL') AS order_type,
                    COALESCE(o.patient_mobile, '') AS patient_mobile,
                    COALESCE(o.party_id::text, '') AS party_id,
                    COALESCE(o.customer_order_no, '') AS customer_order_no,
                    COUNT(ol.id) AS line_count,
                    COALESCE(SUM(COALESCE(ol.discount_amount, 0)), 0) AS total_discount,
                    COALESCE(SUM(COALESCE(ol.unit_price, 0)
                                 * COALESCE(ol.quantity, 0)), 0)     AS lines_gross_calc
                FROM orders o
                LEFT JOIN order_lines ol
                  ON ol.order_id = o.id AND COALESCE(ol.is_deleted, FALSE) = FALSE
                WHERE COALESCE(o.is_deleted, FALSE) = FALSE
                  AND UPPER(COALESCE(o.order_type, '')) != 'CONSULTATION'
                {closed_filter}
                GROUP BY o.id, o.order_no, o.patient_name, o.party_name,
                         o.status, o.created_at, o.total_value, o.order_type,
                         o.patient_mobile, o.party_id, o.customer_order_no
                ORDER BY o.created_at DESC
                LIMIT %(lim)s
            """, {"lim": limit})
            except Exception:
                conn.rollback()
                cur.execute(f"""
                SELECT
                    o.id::text AS order_id,
                    o.id::text AS id,
                    o.order_no,
                    COALESCE(o.patient_name, o.party_name, '\u2014') AS patient_name,
                    COALESCE(o.party_name, '')     AS party_name,
                    COALESCE(o.status, 'PENDING') AS status,
                    o.created_at,
                    COALESCE(o.total_value, 0)    AS total_value,
                    COALESCE(o.order_type, 'RETAIL') AS order_type,
                    COALESCE(o.patient_mobile, '') AS patient_mobile,
                    COALESCE(o.party_id::text, '') AS party_id,
                    COALESCE(o.customer_order_no, '') AS customer_order_no,
                    COUNT(ol.id) AS line_count,
                    0::numeric AS total_discount,
                    0::numeric AS lines_gross_calc
                FROM orders o
                LEFT JOIN order_lines ol
                  ON ol.order_id = o.id AND COALESCE(ol.is_deleted, FALSE) = FALSE
                WHERE COALESCE(o.is_deleted, FALSE) = FALSE
                  AND UPPER(COALESCE(o.order_type, '')) != 'CONSULTATION'
                {closed_filter}
                GROUP BY o.id, o.order_no, o.patient_name, o.party_name,
                         o.status, o.created_at, o.total_value, o.order_type,
                         o.patient_mobile, o.party_id, o.customer_order_no
                ORDER BY o.created_at DESC
                LIMIT %(lim)s
            """, {"lim": limit})

            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))

                # Defensive net total. We compare orders.total_value to
                # sum(unit_price * quantity) — the TRUE pre-discount gross.
                #   - total_value ≈ gross AND discount > 0 → legacy gross row,
                #     subtract discount to get net.
                #   - total_value < gross by ≈ discount      → already net,
                #     leave alone.
                # The previous comparison (against sum(total_price)) failed
                # once retail started writing net into total_price, because
                # then both gross-era and net-era rows had total_value ==
                # sum(total_price) and the test couldn't distinguish them.
                try:
                    _tv   = float(rec.get("total_value") or 0)
                    _disc = float(rec.get("total_discount") or 0)
                    _gc   = float(rec.get("lines_gross_calc") or 0)
                    if _disc > 0 and _gc > 0 and abs(_tv - _gc) < 1.0:
                        # total_value looks gross — subtract discount
                        rec["net_total_value"] = round(_tv - _disc, 2)
                    else:
                        # total_value already reflects net (or no discount)
                        rec["net_total_value"] = round(_tv, 2)
                except Exception:
                    rec["net_total_value"] = float(rec.get("total_value") or 0)

                rec.setdefault("lines", [])
                rec["_summary_only"] = True
                out.append(rec)
            return out
    except Exception as _e:
        import logging; logging.warning(f"[BO] load_orders_summary: {_e}")
        return []
    finally:
        cur.close()


@st.cache_data(ttl=15, show_spinner=False)
def load_single_order(order_id: str):
    """
    Load FULL detail for ONE order (header + lines + product join).
    Called lazily when a user opens an order card.
    Cache TTL=15s so changes reflect quickly.
    Returns a fully hydrated order dict matching the existing bo_active_orders format,
    so render_order_detail() and all downstream functions work unchanged.
    """
    from modules.core.system_observer import perf_step
    conn = get_connection()
    cur  = conn.cursor()
    try:
        with perf_step("Backoffice resolve single order", category="loader", detail=str(order_id or "")[:80]):
            order_ref = str(order_id or "").strip()
            if not order_ref:
                return None

            import uuid as _uuid
            try:
                _uuid.UUID(order_ref)
                cur.execute(
                    "SELECT order_no FROM orders WHERE id=%s::uuid LIMIT 1",
                    (order_ref,)
                )
            except Exception:
                conn.rollback()
                cur.execute(
                    "SELECT order_no FROM orders WHERE order_no=%s LIMIT 1",
                    (order_ref,)
                )
            row = cur.fetchone()
            if not row:
                return None
            order_no = row[0]
    finally:
        cur.close()

    # Load only this order. The older path hydrated every recent order and then
    # filtered in Python, which made Backoffice detail/billing jumps feel slow.
    try:
        from modules.backoffice.backoffice_helpers import load_orders_from_database as _load_full_order
        with perf_step("Backoffice hydrate single order", category="loader", detail=str(order_no)):
            rows = _load_full_order(limit=1, include_closed=True, order_no=order_no) or []
        return rows[0] if rows else None
    except Exception:
        return None



@st.cache_data(ttl=30, show_spinner=False)
def load_orders_from_database():
    """
    Load orders + lines from DB, mapping real column names to UI field names.

    DB schema (verified from backoffice_backup.backup):
      orders:      id, order_no, order_type, party_id, status, total_items,
                   total_value, created_at, party_name, patient_name,
                   patient_mobile, customer_order_no
      order_lines: id, order_id, product_id, sph, cyl, axis, add_power,
                   eye_side, quantity, unit_price, total_price, status,
                   lens_params, boxing_params, allocated_qty, ready_qty,
                   billed_qty, dispatched_qty
      products:    id, product_code, product_name, brand, main_group, category,
                   material, index_value, coating, colour, is_active, created_at,
                   wear_schedule, gender, unit, is_batch_applicable, is_eye_specific,
                   hsn_code, coating_type, lens_category, brand_group, updated_at,
                   gst_percent, box_size, allow_loose
                   NOTE: NO selling_price column in products

    UI field aliases:
      DB quantity     -> billing_qty   (master qty field throughout backoffice_ui)
      DB total_price  -> billing_total (line total field throughout backoffice_ui)
      manufacturing_route extracted from lens_params jsonb -> top-level key
    """
    conn = get_connection()
    cur = conn.cursor()
    orders = {}

    try:
        # ---------------------------------------------------------------
        # 1. Load order headers
        # ---------------------------------------------------------------
        # Try with order_source first; fall back if column not yet migrated
        try:
            cur.execute("""
                SELECT
                    id, order_no, patient_name, status, created_at,
                    party_name, party_id,
                    COALESCE(order_type, 'RETAIL') AS order_type,
                    total_value,
                    patient_mobile, customer_order_no,
                    COALESCE(order_source, order_type, 'RETAIL') AS order_source,
                    COALESCE(extra_data, '{}'::jsonb) AS extra_data
                FROM orders
                WHERE COALESCE(is_deleted,FALSE)=FALSE
                  AND UPPER(COALESCE(order_type, '')) != 'CONSULTATION'
                ORDER BY created_at DESC LIMIT 50
            """)
            _has_order_source = True
        except Exception:
            conn.rollback()
            cur.execute("""
                SELECT
                    id, order_no, patient_name, status, created_at,
                    party_name, party_id,
                    COALESCE(order_type, 'RETAIL') AS order_type,
                    total_value,
                    patient_mobile, customer_order_no,
                    '{}'::jsonb AS extra_data
                FROM orders
                WHERE COALESCE(is_deleted,FALSE)=FALSE
                  AND UPPER(COALESCE(order_type, '')) != 'CONSULTATION'
                ORDER BY created_at DESC LIMIT 50
            """)
            _has_order_source = False

        for row in cur.fetchall():
            order_id = str(row[0])
            orders[order_id] = {
                "order_id":          order_id,
                "order_no":          row[1],
                "patient_name":      row[2] or "",
                "status":            row[3] or "PENDING",
                "created_at":        row[4],
                "party_name":        row[5] or "",
                "party_id":          str(row[6]) if row[6] else None,
                "order_type":        row[7] or "RETAIL",  # COALESCE in query, but belt+suspenders
                "total_value":       float(row[8] or 0),
                "patient_mobile":    row[9] or "",
                "customer_order_no": row[10] or "",
                "order_source":      (row[11] if _has_order_source else row[7]) or "unknown",
                "extra_data":        _parse_jsonb(row[12] if _has_order_source else row[11]),
                "stock_lines":       [],
                "inhouse_lines":     [],
                "lab_order_lines":   [],
                "service_lines":     [],
                "lines":             [],
                # Mark as loaded from DB so persistence layer knows it exists
                "_existed_in_db":    True,
                # Carry status so first save after load can diff correctly
                "_prev_status":      row[3] or "PENDING",
            }

        if not orders:
            return []

        # ---------------------------------------------------------------
        # 2. Load order lines — JOIN products for all fields the UI needs.
        #
        #    IMPORTANT: products table has NO selling_price column.
        #    Price fallback in the allocation window uses unit_price from
        #    order_lines (already loaded as row[9]) and batch prices from
        #    inventory_stock (fetched separately by get_available_stock).
        #
        #    Row index map (keep in sync with dict below):
        #      0  ol.id
        #      1  ol.order_id
        #      2  product_name
        #      3  p.brand
        #      4  p.main_group
        #      5  p.material
        #      6  ol.eye_side
        #      7  billing_qty       (DB: quantity)
        #      8  allocated_qty
        #      9  unit_price
        #      10 billing_total     (DB: total_price)
        #      11 ol.sph
        #      12 ol.cyl
        #      13 ol.axis
        #      14 ol.add_power
        #      15 ol.status
        #      16 ol.lens_params
        #      17 ol.boxing_params
        #      18 ready_qty
        #      19 billed_qty
        #      20 dispatched_qty
        #      21 ol.product_id
        #      22 p.unit
        #      23 box_size
        #      24 p.is_batch_applicable
        #      25 p.is_eye_specific
        #      26 p.lens_category
        #      27 gst_percent
        #      28 p.allow_loose
        #      29 p.product_code
        # ---------------------------------------------------------------
        # Run full query with pricing columns; fall back without them if they don't exist yet
        _pricing_cols_available = True
        try:
            cur.execute("""
            SELECT
                ol.id,                                                   -- 0
                ol.order_id,                                             -- 1
                COALESCE(p.product_name, 'Unknown Product'),             -- 2
                p.brand,                                                 -- 3
                p.main_group,                                            -- 4
                p.material,                                              -- 5
                ol.eye_side,                                             -- 6
                COALESCE(ol.quantity, 0)          AS billing_qty,        -- 7
                COALESCE(ol.allocated_qty, 0)     AS allocated_qty,      -- 8
                COALESCE(ol.unit_price, 0)        AS unit_price,         -- 9
                COALESCE(ol.total_price, 0)       AS billing_total,      -- 10
                ol.sph,                                                  -- 11
                ol.cyl,                                                  -- 12
                ol.axis,                                                 -- 13
                ol.add_power,                                            -- 14
                ol.status,                                               -- 15
                ol.lens_params,                                          -- 16
                ol.boxing_params,                                        -- 17
                COALESCE(ol.ready_qty, 0),                               -- 18
                COALESCE(ol.billed_qty, 0),                              -- 19
                COALESCE(ol.dispatched_qty, 0),                          -- 20
                ol.product_id,                                           -- 21
                p.unit,                                                  -- 22  BOX or PCS
                COALESCE(p.box_size, 0)           AS box_size,           -- 23
                p.is_batch_applicable,                                   -- 24
                p.is_eye_specific,                                       -- 25
                p.lens_category,                                         -- 26
                COALESCE(p.gst_percent, 0)        AS gst_percent,        -- 27
                p.allow_loose,                                           -- 28
                p.product_code,                                          -- 29
                -- pricing columns (added by 003_add_pricing_columns.sql migration)
                -- COALESCE to 0 handles both NULL and column-not-yet-backfilled
                COALESCE(ol.gst_amount, 0)        AS gst_amount,         -- 30
                COALESCE(ol.discount_percent, 0)  AS discount_percent,   -- 31
                COALESCE(ol.discount_amount, 0)   AS discount_amount,    -- 32
                COALESCE(ol.is_service_line, FALSE) AS is_service_line,  -- 33
                ol.production_ref                                      -- 34
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.order_id = ANY(%s::uuid[])
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, (list(orders.keys()),))
        except Exception as _qe:
            # Pricing columns don't exist yet — retry without them
            if "gst_amount" in str(_qe) or "discount_percent" in str(_qe) or "column" in str(_qe).lower():
                _pricing_cols_available = False
                cur.execute("""
                    SELECT
                        ol.id, ol.order_id,
                        COALESCE(p.product_name, 'Unknown Product'),
                        p.brand, p.main_group, p.material,
                        ol.eye_side,
                        COALESCE(ol.quantity, 0)        AS billing_qty,
                        COALESCE(ol.allocated_qty, 0)   AS allocated_qty,
                        COALESCE(ol.unit_price, 0)      AS unit_price,
                        COALESCE(ol.total_price, 0)     AS billing_total,
                        ol.sph, ol.cyl, ol.axis, ol.add_power, ol.status,
                        ol.lens_params, ol.boxing_params,
                        COALESCE(ol.ready_qty, 0),
                        COALESCE(ol.billed_qty, 0),
                        COALESCE(ol.dispatched_qty, 0),
                        ol.product_id, p.unit,
                        COALESCE(p.box_size, 0)         AS box_size,
                        p.is_batch_applicable, p.is_eye_specific,
                        p.lens_category,
                        COALESCE(p.gst_percent, 0)      AS gst_percent,
                        p.allow_loose, p.product_code
                    FROM order_lines ol
                    LEFT JOIN products p ON p.id = ol.product_id
                    WHERE ol.order_id = ANY(%s::uuid[])
                      AND COALESCE(ol.is_deleted, FALSE) = FALSE
                """, (list(orders.keys()),))
            else:
                raise

        for row in cur.fetchall():
            lens_params   = _parse_jsonb(row[16])
            boxing_params = _parse_jsonb(row[17])

            # Derive manufacturing_route:
            # 1. lens_params["manufacturing_route"] — written by persistence on every save
            # 2. None if not set — preserved as None so filters like
            #    == "EXTERNAL_LAB" / == "VENDOR" work correctly.
            # NEVER default to "LAB" — that masks unassigned and breaks route filters.
            manufacturing_route = (
                lens_params.get("manufacturing_route")
                if isinstance(lens_params, dict)
                else None
            ) or None

            eye_side = str(row[6]).strip() if row[6] else None

            # box_size=0 in DB means "not set" — normalise to 1 to keep division safe
            raw_box_size = int(row[23] or 0)
            box_size     = raw_box_size if raw_box_size > 0 else 1

            line = {
                # Identity
                "line_id":             str(row[0]),
                "order_id":            str(row[1]),
                "product_id":          str(row[21]) if row[21] else None,
                "production_ref":      str(row[34] or "") if len(row) > 34 else "",

                # Product master fields (hydrated via JOIN)
                "product_name":        row[2] or "Unknown Product",
                "brand":               row[3] or "",
                "main_group":          row[4] or "",
                "material":            row[5] or "",
                "product_code":        row[29] or "",
                "lens_category":       row[26] or "",

                # Unit/box fields — critical for price-per-PCS normalisation
                # in render_allocation_window (backoffice_ui lines 1015-1073)
                "unit":                str(row[22] or "PCS").upper(),
                "box_size":            box_size,
                # selling_price: products table has NO such column.
                # Allocation window falls back through unit_price then batch prices.
                "selling_price":       0.0,

                # Product flags
                "is_batch_applicable": bool(row[24]),
                "is_eye_specific":     bool(row[25]),
                "gst_percent":         float(row[27] or 0),
                "allow_loose":         bool(row[28]),

                # Eye & power
                "eye_side":            eye_side,
                "is_service_line":     bool(row[33]) if (len(row) > 33 and row[33] is not None) else (
                    str(eye_side or "").upper() in ("S", "SERVICE")
                ),
                "sph":                 float(row[11]) if row[11] is not None else None,
                "cyl":                 float(row[12]) if row[12] is not None else None,
                "axis":                int(row[13])   if row[13] is not None else None,
                "add_power":           float(row[14]) if row[14] is not None else None,

                # Quantity — aliased to UI names
                "billing_qty":         int(row[7] or 0),    # DB: quantity
                "allocated_qty":       int(row[8] or 0),
                "ready_qty":           int(row[18] or 0),
                "billed_qty":          int(row[19] or 0),
                "dispatched_qty":      int(row[20] or 0),

                # Pricing — aliased to UI names
                "unit_price":          float(row[9] or 0),
                "billing_total":       float(row[10] or 0), # DB: total_price

                # Line status
                "status":              row[15],

                # JSON blobs
                "lens_params":         lens_params,
                "boxing_params":       boxing_params,

                # Unpack surfacing_data from lens_params so job card UI sees it on reload
                "surfacing_data":      lens_params.get("surfacing_data") if isinstance(lens_params, dict) else None,

                # Derived — used by UI for routing and grouping
                "manufacturing_route": manufacturing_route,

                # Triggers lazy workflow refresh in render_order_detail()
                "_needs_refresh":      True,
                # Always False on fresh DB load — only set True by SAVE button
                # Prevents stale session lock persisting across reloads
                "pricing_locked":     False,
            }

            # ── Compute GST amount: use DB value if saved, else back-calc ──────
            _total  = line["billing_total"]
            _gst_pc = line["gst_percent"]
            if _pricing_cols_available and len(row) > 30:
                _db_gst     = float(row[30]) if row[30] else 0.0
                _db_disc_pc = float(row[31]) if row[31] else 0.0
                _db_disc_am = float(row[32]) if row[32] else 0.0
                if _db_gst > 0:
                    line["gst_amount"] = _db_gst
                elif _total > 0 and _gst_pc > 0:
                    line["gst_amount"] = round(_total - (_total / (1 + _gst_pc / 100)), 2)
                else:
                    line["gst_amount"] = 0.0
                line["discount_percent"] = _db_disc_pc
                line["discount_amount"]  = _db_disc_am
            else:
                # Pricing columns not in DB yet — back-calc GST from total+rate
                if _total > 0 and _gst_pc > 0:
                    line["gst_amount"] = round(_total - (_total / (1 + _gst_pc / 100)), 2)
                else:
                    line["gst_amount"] = 0.0
                line["discount_percent"] = 0.0
                line["discount_amount"]  = 0.0

            order = orders.get(line["order_id"])
            if not order:
                continue

            order["lines"].append(line)

            # Route-based grouping for UI tabs
            # SERVICE lines (eye_side=S or is_service_line) go to service_lines.
            # VENDOR and EXTERNAL_LAB both go to lab_order_lines (procurement bucket).
            # None (unassigned) also goes there — assignment panel will categorize it.
            _eye_s   = str(line.get("eye_side") or "").upper()
            _is_svc  = bool(line.get("is_service_line"))  # already set in line dict above
            _route   = manufacturing_route or ""

            # Frames are NEVER production items — route them to stock/procurement
            # regardless of manufacturing_route value. Only ophthalmic prescription
            # lenses (eye_side R/L with sph values) go to inhouse_lines.
            _is_frame = (
                "frame" in str(line.get("main_group") or "").lower()
                or "sunglass" in str(line.get("main_group") or "").lower()
                or (str(line.get("eye_side") or "").upper() in ("B","OTHER","X","")
                    and not line.get("sph")
                    and str(line.get("main_group") or "").lower() not in ("ophthalmic",""))
            )
            _is_rx_lens = (
                str(line.get("eye_side") or "").upper() in ("R","L","RIGHT","LEFT")
                and line.get("sph") is not None
                and not _is_frame
            )

            if _is_svc:
                order.setdefault("service_lines", []).append(line)
            elif _is_frame:
                # Frame/stock item — goes to stock regardless of route
                order["stock_lines"].append(line)
            elif _route == "STOCK":
                order["stock_lines"].append(line)
            elif _route == "INHOUSE" and _is_rx_lens:
                order["inhouse_lines"].append(line)
            elif _route == "INHOUSE" and not _is_rx_lens:
                # Non-lens marked as INHOUSE (shouldn't happen but guard it)
                order["stock_lines"].append(line)
            else:
                order["lab_order_lines"].append(line)

        # ────────────────────────────────────────────────────────────────
        # POST-LOAD: defensive net rollup
        # ────────────────────────────────────────────────────────────────
        # Some retail orders saved order_lines.total_price as GROSS
        # (pre-discount) — the punching INSERT uses ln.get("total_price")
        # which discount_engine never restamped. Wholesale uses
        # restamp_line_totals so total_price is already net there.
        #
        # Heuristic: if discount_amount > 0 AND billing_total ≈ unit_price*qty
        # (i.e. it looks gross), subtract discount_amount to make it net.
        # This is idempotent on already-net rows because for those
        # billing_total < unit_price*qty by exactly the discount, so the
        # heuristic does not fire.
        #
        # Result: billing_total throughout backoffice is the actual
        # post-discount line total, and order["total_value"] reflects what
        # the customer pays. order["total_discount"] carries the rolled-up
        # discount for display.
        for _o in orders.values():
            try:
                from modules.backoffice.backoffice_helpers import auto_heal_zero_priced_sibling_lines
                auto_heal_zero_priced_sibling_lines(_o, persist=True)
            except Exception:
                pass
            _disc_total = 0.0
            _net_total  = 0.0
            for _ln in _o.get("lines", []):
                _bt   = float(_ln.get("billing_total") or 0)
                _up   = float(_ln.get("unit_price") or 0)
                _q    = int(_ln.get("billing_qty") or 0)
                _da   = float(_ln.get("discount_amount") or 0)
                _gross_calc = round(_up * _q, 2)
                # Defensive net adjustment for legacy gross rows
                if _da > 0 and _gross_calc > 0 and abs(_bt - _gross_calc) < 0.5:
                    _bt_net = round(_bt - _da, 2)
                    if _bt_net < 0:
                        _bt_net = 0.0
                    _ln["billing_total_gross"] = _bt
                    _ln["billing_total"]       = _bt_net
                    _bt = _bt_net
                _disc_total += _da
                _net_total  += _bt
            _o["total_discount"]   = round(_disc_total, 2)
            _o["total_value_net"]  = round(_net_total, 2)
            # Override total_value with the net so backoffice cards/headers
            # show the actual payable amount. The original gross is kept
            # under total_value_gross for any caller that needs it.
            try:
                _o["total_value_gross"] = float(_o.get("total_value") or 0)
            except Exception:
                _o["total_value_gross"] = 0.0
            _o["total_value"] = round(_net_total, 2) if _net_total > 0 else _o.get("total_value", 0)

        return list(orders.values())

    finally:
        cur.close()
        conn.close()
