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
                    party_name, party_id, order_type, total_value,
                    patient_mobile, customer_order_no,
                    COALESCE(order_source, order_type) AS order_source
                FROM orders WHERE COALESCE(is_deleted,FALSE)=FALSE ORDER BY created_at DESC LIMIT 50
            """)
            _has_order_source = True
        except Exception:
            conn.rollback()
            cur.execute("""
                SELECT
                    id, order_no, patient_name, status, created_at,
                    party_name, party_id, order_type, total_value,
                    patient_mobile, customer_order_no
                FROM orders WHERE COALESCE(is_deleted,FALSE)=FALSE ORDER BY created_at DESC LIMIT 50
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
                "order_type":        row[7],
                "total_value":       float(row[8] or 0),
                "patient_mobile":    row[9] or "",
                "customer_order_no": row[10] or "",
                "order_source":      (row[11] if _has_order_source else row[7]) or "unknown",
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
                COALESCE(ol.is_service_line, FALSE) AS is_service_line   -- 33
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

            if _is_svc:
                order.setdefault("service_lines", []).append(line)
            elif _route == "STOCK":
                order["stock_lines"].append(line)
            elif _route == "INHOUSE":
                order["inhouse_lines"].append(line)
            else:
                order["lab_order_lines"].append(line)

        return list(orders.values())

    finally:
        cur.close()
        conn.close()
