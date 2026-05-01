"""
Backoffice Helpers Module
Utility functions, formatters, and calculations

Contains:
- Formatting functions (fmt_num, fmt_signed)
- Power calculations (compute_jobcard_power)
- Stock batch resolution
- Widget key management
- Price lookups
- Order categorization
- Database loading
"""

import streamlit as st
import pandas as pd
import datetime
import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Import core dependencies
from modules.power_engine import (
    vertex_correct_spherical,
    vertex_correct_toric
)
from modules.batch_manager import get_available_stock
from modules.utils.power_normalizer import normalize_power
from modules.sql_adapter import (
    read_product_master,
    fetch_backoffice_orders,
    fetch_orders_with_lines   #  ADD THIS
)

#  Lazy import to avoid circular dependency at module load time
# refresh_line_state is imported inside functions that use it below


# ====================================================================
# FORMATTING HELPERS
# ====================================================================

def fmt_num(val, pattern="{:.2f}", default="N/A"):
    """Format numeric value with pattern  handles None and NaN"""
    try:
        if val is None:
            return default
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return default
        return pattern.format(f)
    except (TypeError, ValueError):
        return default


def fmt_signed(val, default="N/A"):
    """Format numeric value with +/- sign  handles None and NaN"""
    return fmt_num(val, "{:+.2f}", default)


def get_display_order_id(order: Dict) -> str:
    """
    Returns the primary order identifier used for lookups (order_no / PO-XXXX).
    NOTE: for display with the sequential number, use get_display_label(order).
    """
    return (
        order.get('final_order_id')
        or order.get('order_no')
        or order.get('order_id')
        or order.get('provisional_order_id', 'UNKNOWN')
    )


def get_display_label(order: Dict) -> str:
    """
    Human-readable order label shown in the UI.
    Shows sequential number if available: #0042 · PO-11C0DC84
    Falls back to just the order_no.
    """
    order_no = get_display_order_id(order)
    seq      = order.get("display_order_no")
    if seq:
        return f"#{int(seq):04d} · {order_no}"
    return order_no


# ====================================================================
# POWER + BATCH HELPERS (JOB CARD / STOCK)
# ====================================================================

def compute_jobcard_power(line: Dict) -> Dict:
    """
    Compute manufacturing power:
    - Contact lenses: Apply vertex correction (effectivity)
    - Ophthalmic lenses: Use spectacle power directly (no effectivity)
    """
    
    def safe_int_axis(axis_val):
        """Safely convert axis to int, handling None and NaN"""
        if axis_val is None:
            return None
        if isinstance(axis_val, float) and math.isnan(axis_val):
            return None
        try:
            return int(axis_val)
        except (ValueError, TypeError):
            return None
    
    def is_empty_power(val):
        """Check if a power value is empty (None, 0, or NaN)"""
        if val is None or val == 0:
            return True
        if isinstance(val, float) and math.isnan(val):
            return True
        return False
    
    sph = line.get('sph')
    cyl = line.get('cyl', 0)
    axis = line.get('axis')
    
    if sph is None:
        return {}
    
    # Determine product type
    main_group = (line.get('main_group') or '').lower()
    product_type = ""
    is_contact_lens = 'contact' in main_group or product_type == 'contact_lens'
    use_effective = line.get("use_effective_power", False)
    
    #  FIX: For ophthalmic lenses, NO vertex correction (use spectacle power directly)
    if not is_contact_lens:

        # Spherical (no cylinder)
        if is_empty_power(cyl):
            return {
                'sph_out': float(sph),
                'cyl_out': 0.0,
                'axis_out': None
            }

        # Toric (with cylinder)
        else:
            return {
                'sph_out': float(sph),
                'cyl_out': float(cyl),
                'axis_out': safe_int_axis(axis)
            }

    #  Contact lenses
    if is_contact_lens:

        # With effective power (vertex)
        if use_effective:

            # Spherical
            if is_empty_power(cyl):
                return {
                    'sph_out': vertex_correct_spherical(sph),
                    'cyl_out': 0.0,
                    'axis_out': None
                }

            # Toric
            sph_o, cyl_o, axis_o = vertex_correct_toric(sph, cyl, axis)

            return {
                'sph_out': sph_o,
                'cyl_out': cyl_o,
                'axis_out': axis_o
            }

        # Without effective  copy RX
        else:

            if is_empty_power(cyl):
                return {
                    'sph_out': float(sph),
                    'cyl_out': 0.0,
                    'axis_out': None
                }

            return {
                'sph_out': float(sph),
                'cyl_out': float(cyl),
                'axis_out': safe_int_axis(axis)
            }


def resolve_stock_batch(line: Dict) -> Dict:
    """
    Apply batch logic for ALL stock / job-card items
    """
    product_id = line.get('product_id')
    if not product_id:
        return {'batch_status': 'NO_PRODUCT'}

    #  Ensure product_id is string for UUID compatibility
    product_id = str(product_id)

    # -------------------------------------------------
    # Use manufacturing power first, fallback to RX
    # -------------------------------------------------
    sph = normalize_power(line.get("sph_out") or line.get("sph"))
    cyl = normalize_power(line.get("cyl_out") or line.get("cyl"))
    axis = normalize_power(line.get("axis_out") or line.get("axis"))
    add_power = normalize_power(line.get("add_power"))
    eye_side = line.get("eye_side")

    # Axis 0 must behave like NULL in DB
    if axis == 0:
        axis = None

    # -------------------------------------------------
    # Call stock engine
    # -------------------------------------------------
    stock_df = get_available_stock(
        product_id=product_id,
        sph=sph,
        cyl=cyl,
        axis=axis,
        add_power=add_power,
        eye_side=eye_side
    )

    # -------------------------------------------------
    # No stock case
    # -------------------------------------------------
    if stock_df.empty:
        return {"batch_status": "NO_STOCK"}

    row = stock_df.iloc[0]

    return {
        "batch_status": "ALLOCATED",
        "batch_no": row.get("batch_no"),
        "source": row.get("source"),
        "available_qty": row.get("available_qty")
    }

def power_key(order, line_idx, field):
    """Generate unique widget key for power fields"""
    order_id = get_display_order_id(order)
    return f"{field}_{order_id}_{line_idx}"


def sync_power_to_ui(line, line_idx, order):
    """
    Sync backend power  Streamlit widgets
    (Single source of truth)
    """

    sph_key = power_key(order, line_idx, "sph_out")
    cyl_key = power_key(order, line_idx, "cyl_out")
    axis_key = power_key(order, line_idx, "axis_out")

    st.session_state[sph_key] = float(line.get("sph_out") or 0)
    st.session_state[cyl_key] = float(line.get("cyl_out") or 0)

    axis = line.get("axis_out")
    st.session_state[axis_key] = int(axis) if axis is not None else 0


def force_power_refresh(line, line_idx, order):
    """
    Hard refresh manufacturing power widgets
    (Fixes Streamlit stale UI issue)
    """

    sph_key = power_key(order, line_idx, "sph_out")
    cyl_key = power_key(order, line_idx, "cyl_out")
    axis_key = power_key(order, line_idx, "axis_out")

    # Remove old widget state
    for k in (sph_key, cyl_key, axis_key):
        if k in st.session_state:
            del st.session_state[k]

    # Re-initialize from line
    st.session_state[sph_key] = float(line.get("sph_out") or 0)
    st.session_state[cyl_key] = float(line.get("cyl_out") or 0)

    axis = line.get("axis_out")
    st.session_state[axis_key] = int(axis) if axis is not None else 0


# ====================================================================
# PRICE LOOKUPS
# ====================================================================

def get_max_historical_price(product_id: str, sph=None, cyl=None, axis=None, add_power=None, eye_side=None) -> float:
    """
    Universal pricing logic for pending / non-stock items
    """

    try:
        product_id = str(product_id)

        # ==================================================
        # 1 Try batch prices (any power)
        # ==================================================

        stock_df = get_available_stock(
            product_id=product_id,
            sph=None,
            cyl=None,
            axis=None,
            add_power=None,
            eye_side=None
        )

        if not stock_df.empty:

            for col in ['unit_price', 'selling_price', 'price', 'rate', 'mrp']:

                if col in stock_df.columns:
                    prices = stock_df[col].dropna()
                    prices = prices[prices > 0]

                    if not prices.empty:
                        return float(prices.max())


        # ==================================================
        # 2 Product Master Fallback (ALL FIELDS)
        # ==================================================

        products_df = read_product_master()

        if not products_df.empty:

            row = products_df[
                products_df['product_id'].astype(str) == product_id
            ]

            if not row.empty:

                product = row.iloc[0]

                # Try all possible price fields
                price_fields = [
                    'selling_price',
                    'sale_price',
                    'price',
                    'rate',
                    'unit_price',
                    'mrp',
                    'mrp_price',
                    'mrp_rate'
                ]

                for field in price_fields:
                    if field in product and pd.notna(product[field]):

                        val = float(product[field])

                        if val > 0:
                            return val


        # ==================================================
        # 3 Emergency fallback: Last known order price
        # ==================================================

        from modules.sql_adapter import fetch_last_product_price

        try:
            last_price = fetch_last_product_price(product_id)

            if last_price and last_price > 0:
                return float(last_price)

        except:
            pass


        return 0.0


    except Exception as e:
        st.warning(f"Price lookup failed: {str(e)}")
        return 0.0


# ====================================================================
# DATABASE AND ORDER MANAGEMENT
# ====================================================================

def _fetch_display_numbers(order_nos: list) -> dict:
    """
    Safely fetch display_order_no for a list of orders.
    Returns {} if column doesn't exist yet (migration pending).
    """
    if not order_nos:
        return {}
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT order_no, display_order_no FROM orders "
            "WHERE order_no = ANY(%(nos)s) AND display_order_no IS NOT NULL",
            {"nos": order_nos}
        )
        return {r["order_no"]: r["display_order_no"] for r in (rows or [])}
    except Exception:
        # Column doesn't exist yet — migration hasn't run, silently skip
        return {}




@st.cache_data(ttl=60, show_spinner=False)
def load_orders_from_database(limit: int = 10, offset: int = 0, include_closed: bool = False):

    try:

        logger.info(f"[BO] Loading orders limit={limit} offset={offset} closed={include_closed}")

        # ---------------------------------------------
        # 1 Fetch headers only (fast — no JOIN)
        # ---------------------------------------------
        orders_df = fetch_backoffice_orders(limit=limit, offset=offset, include_closed=include_closed)

        if orders_df is None or orders_df.empty:
            return []

        order_nos = orders_df["order_no"].tolist()

        # ---------------------------------------------
        # 2 Batch fetch ALL lines
        # ---------------------------------------------
        lines_df = fetch_orders_with_lines(order_nos)

        # ✅ FIX: Don't bail on empty lines_df — orders may have no lines yet
        # (e.g. just created). Fall back to building header-only orders from orders_df.
        if lines_df is None or lines_df.empty:
            lines_df = pd.DataFrame()

        # ---------------------------------------------
        # 3 Build product map ONCE
        # ---------------------------------------------
        products_df = read_product_master()

        product_map = {}

        if products_df is not None and not products_df.empty:
            products_df["product_id"] = products_df["product_id"].astype(str)

            for _, p in products_df.iterrows():
                product_map[str(p["product_id"])] = p.to_dict()

        # ---------------------------------------------
        # 4 Build orders in memory
        # ---------------------------------------------
        orders_map = {}

        # ✅ FIX: Pre-populate orders_map from the headers DataFrame so that
        # orders with zero lines still appear in the backoffice list.
        for _, hrow in orders_df.iterrows():
            ono = hrow.get("order_no")
            if ono and ono not in orders_map:
                _raw_status1 = hrow.get("status") or hrow.get("order_status") or "PENDING"
                # Use canonical alias map from order_status_live
                try:
                    from modules.backoffice.order_status_live import _ALIAS as _OSL_ALIAS
                    _status1 = _OSL_ALIAS.get(_raw_status1, _raw_status1)
                except Exception:
                    _status1 = ("PENDING" if _raw_status1 in ("PENDING_VALIDATION","PROVISIONAL","ORDER_SAVED")
                                else _raw_status1)
                orders_map[ono] = {
                    "id":           str(hrow.get("order_id") or ""),  # UUID for DB queries
                    "order_no":         ono,
                    "display_order_no": hrow.get("display_order_no"),
                    "created_at":       hrow.get("created_at"),
                    "order_date":       hrow.get("created_at"),
                    "status":       _status1,
                    "patient_name": hrow.get("patient_name"),
                    "party_name":   hrow.get("party_name", ""),
                    "order_type":   hrow.get("order_type", "RETAIL"),
                    "order_source": hrow.get("order_source", ""),
                    "total_value":  float(hrow.get("total_value") or 0),
                    "party_id":     str(hrow.get("party_id") or ""),
                    "patient_mobile": hrow.get("patient_mobile") or "",
                    "customer_order_no": hrow.get("customer_order_no") or "",
                    "lines":        [],
                }

        for _, row in lines_df.iterrows():

            ono = row["order_no"]

            if ono not in orders_map:
                # Shouldn't happen after pre-populate, but keep as safety net
                orders_map[ono] = {
                    "id":           str(row.get("order_id") or ""),  # UUID for DB queries
                    "order_no":         ono,
                    "display_order_no": row.get("display_order_no"),
                    "created_at":       row.get("created_at"),
                    "order_date":       row.get("created_at"),
                    "status":       (lambda s: ((__import__("modules.backoffice.order_status_live", fromlist=["_ALIAS"])._ALIAS.get(s, s)) if True else s))(
                                        row.get("status") or row.get("order_status") or "PENDING"
                                    ),
                    "patient_name": row.get("patient_name"),
                    "party_name":   row.get("party_name", ""),
                    "order_type":   row.get("order_type", "RETAIL"),
                    "order_source": row.get("order_source", ""),
                    "total_value":  float(row.get("total_value") or 0),
                    "party_id":     str(row.get("party_id") or ""),
                    "patient_mobile": row.get("patient_mobile") or "",
                    "customer_order_no": row.get("customer_order_no") or "",
                    "lines":        [],
                }

            if pd.notna(row["line_id"]):

                product_id = str(row["product_id"])
                product_meta = product_map.get(product_id, {})

                # Extract manufacturing_route from lens_params JSON
                # (stored inside lens_params JSONB, not a real column)
                def _parse_jsonb_field(val):
                    """Parse JSONB that pandas may return as str, dict, None, or NaN."""
                    import math as _math2
                    if val is None:
                        return {}
                    if isinstance(val, float) and _math2.isnan(val):
                        return {}
                    if isinstance(val, dict):
                        return val
                    if isinstance(val, str) and val.strip():
                        try:
                            import json as _jj
                            return _jj.loads(val) or {}
                        except Exception:
                            return {}
                    return {}

                _lp = _parse_jsonb_field(row.get("lens_params"))
                _bp = _parse_jsonb_field(row.get("boxing_params"))
                _mfg_route = _lp.get("manufacturing_route")
                _sup_oid   = _lp.get("supplier_order_id")

                line = {
                    "id": row["line_id"],
                    "line_id": str(row["line_id"]),   # explicit alias used by _line_key()
                    "product_id": product_id,

                    # PRODUCT MASTER HYDRATION
                    "product_name": product_meta.get("product_name") or row.get("product_name"),
                    "brand":        product_meta.get("brand")        or row.get("brand"),
                    "unit":         product_meta.get("unit"),
                    "box_size":     int(product_meta.get("box_size") or 1),
                    "main_group":   product_meta.get("main_group")   or row.get("main_group"),
                    "category":     product_meta.get("category")    or row.get("category") or \
                                    product_meta.get("main_group")   or row.get("main_group"),
                    # "type" is the user-facing name for "category" (Excel column "Type" → DB "category")
                    "type":          product_meta.get("type") or product_meta.get("category") or row.get("category") or "",
                    "lens_category": product_meta.get("lens_category") or row.get("lens_category") or "",

                    # Power
                    "sph": row["sph"],
                    "cyl": row["cyl"],
                    "axis": row["axis"],
                    "add_power": row["add_power"],
                    "eye_side": row["eye_side"],

                    # Billing
                    "billing_qty":   row["billing_qty"],
                    "billing_total": row["billing_total"],
                    "unit_price":    float(row.get("unit_price") or 0),
                    "allocated_qty": int(row.get("allocated_qty") or 0),
                    "ready_qty":     int(row.get("ready_qty") or 0),

                    # GST — line column is authoritative, product master is fallback
                    "gst_percent": float(
                        row.get("gst_percent") or
                        row.get("product_gst_percent") or
                        product_meta.get("gst_percent") or 0
                    ),
                    "gst_amount": float(row.get("gst_amount") or 0),

                    # Routing — extracted from lens_params JSON (not a DB column)
                    "manufacturing_route": _mfg_route,
                    "supplier_order_id":   _sup_oid,

                    # Status
                    "batch_status": row["batch_status"],
                    "lens_params":  _lp,
                    "boxing_params": _bp,

                    # ── Restore surfacing_data saved by job card ──────────
                    # Persisted under lens_params["surfacing_data"] on DB.
                    # Expose at top level so job card completion check works.
                    "surfacing_data": _lp.get("surfacing_data") or None,

                    "_needs_refresh": True,
                }

                # Compute manufacturing power immediately
                power = compute_jobcard_power(line)
                line.update(power)

                #  RUN WORKFLOW ENGINE (lazy import to avoid circular deps)
                try:
                    from .backoffice_logic import refresh_line_state, update_line_billing
                    refresh_line_state(line)
                    update_line_billing(line)   #  recalculate billing_total from fresh allocation
                except Exception as e:
                    logger.warning(f"Workflow refresh failed: {e}")

                orders_map[ono]["lines"].append(line)

        # ---------------------------------------------
        # 5 Categorize + stamp GST on every line
        # ---------------------------------------------

        # Build gst_lookup once for all orders — queries product_gst_history
        def _make_gst_lookup():
            try:
                from modules.sql_adapter import run_query
                import datetime as _dt
                rows = run_query("""
                    SELECT product_id::text, gst_percent, effective_from
                    FROM product_gst_history
                    ORDER BY effective_from DESC
                """, params=None) or []

                # Build dict: product_id → [(date, gst_percent), ...] sorted DESC
                hist = {}
                for r in rows:
                    pid      = str(r.get("product_id") or "")
                    raw_date = r.get("effective_from")
                    pct      = float(r.get("gst_percent") or 0)
                    # Normalise effective_from to a date object — handles
                    # datetime, date, string (YYYY-MM-DD), and None
                    if isinstance(raw_date, _dt.datetime):
                        eff = raw_date.date()
                    elif isinstance(raw_date, _dt.date):
                        eff = raw_date
                    elif isinstance(raw_date, str):
                        try:
                            eff = _dt.date.fromisoformat(raw_date[:10])
                        except ValueError:
                            eff = None
                    else:
                        eff = None
                    hist.setdefault(pid, []).append((eff, pct))

                # Python-side sort DESC — defensive against query order changes
                for pid in hist:
                    hist[pid].sort(
                        key=lambda x: x[0] or _dt.date.min,
                        reverse=True
                    )

                def lookup(product_id, bill_date):
                    entries = hist.get(str(product_id), [])
                    # Normalise bill_date to date object
                    if isinstance(bill_date, _dt.datetime):
                        bd = bill_date.date()
                    elif isinstance(bill_date, _dt.date):
                        bd = bill_date
                    elif isinstance(bill_date, str):
                        try:
                            bd = _dt.date.fromisoformat(str(bill_date)[:10])
                        except ValueError:
                            bd = _dt.date.today()
                    else:
                        bd = _dt.date.today()

                    for eff, pct in entries:          # DESC — first match wins
                        if eff is None or eff <= bd:
                            return pct
                    return entries[-1][1] if entries else None   # oldest if none match

                return lookup

            except Exception as e:
                logger.warning(f"[BO] gst_lookup unavailable: {e}")
                return None

        gst_lookup_fn = _make_gst_lookup()

        result = []

        for order in orders_map.values():
            # Normalize first — guarantees all fields present before GST stamp
            try:
                from modules.core.order_normalizer import normalize_order
                order, norm_report = normalize_order(order)
                if norm_report.had_issues:
                    logger.warning(f"[BO] Normalizer fixed fields on {order.get('order_no')}: {norm_report.summary()}")
            except Exception as _ne:
                logger.warning(f"[BO] Normalizer failed: {_ne}")
            categorize_order_lines(order)

            # Stamp GST on all lines now so UI shows correct values immediately
            try:
                from modules.pricing.tax_engine import apply_taxes
                import datetime
                all_order_lines = (
                    order.get("stock_lines", []) +
                    order.get("inhouse_lines", []) +
                    order.get("lab_order_lines", []) +
                    order.get("service_lines", []) +   # consultation/eye-testing fees
                    order.get("lines", [])
                )
                # NOTE: tax_input passes the SAME line dict objects as all_order_lines.
                # apply_taxes() mutates each line in-place (stamps gst_percent_used,
                # gst_amount, tax_inclusive, tax_hash). Those mutations propagate back
                # to stock_lines / inhouse_lines / lab_order_lines because Python
                # dicts are passed by reference — no separate copy needed.
                tax_input = {
                    "order_type": order.get("order_type", "RETAIL"),
                    "bill_date":  order.get("order_date") or datetime.date.today(),
                    "net_value":  sum(float(l.get("billing_total") or l.get("total_price") or 0) for l in all_order_lines),
                    "lines":      all_order_lines,   # same objects — mutations are intentional
                    "gst_lookup": gst_lookup_fn,
                }
                apply_taxes(tax_input)
                # After this point: every line in order["stock_lines"] etc.
                # has gst_percent_used, gst_amount, tax_inclusive, tax_hash stamped.
            except Exception as e:
                logger.warning(f"[BO] GST stamp failed for {order.get('order_no')}: {e}")

            # ── Recompute billing_total + total_value at load time ──────────
            # For WHOLESALE: resolve selling_price via batch DB lookup (1 query
            # for all products in this order), apply box logic, recompute totals.
            # For RETAIL: reapply box logic using existing unit_price (MRP).
            _all_order_lines2 = list({
                id(l): l for src in ("stock_lines","inhouse_lines","lab_order_lines","lines")
                for l in (order.get(src) or [])
            }.values())

            _otype_e = (order.get("order_type") or "RETAIL").upper()

            # For WHOLESALE: batch-fetch selling_price for all product_ids once
            _sp_map = {}
            if _otype_e == "WHOLESALE" and _all_order_lines2:
                try:
                    from modules.sql_adapter import run_query as _rqbatch
                    _pids = list({str(l.get("product_id") or "") for l in _all_order_lines2 if l.get("product_id")})
                    if _pids:
                        # inventory_stock.selling_price (batch-level wholesale price)
                        _inv_rows = _rqbatch("""
                            SELECT DISTINCT ON (product_id)
                                   product_id::text, selling_price
                            FROM inventory_stock
                            WHERE product_id::text = ANY(%(pids)s)
                              AND is_active = true
                              AND selling_price IS NOT NULL
                              AND selling_price > 0
                            ORDER BY product_id, created_at DESC
                        """, {"pids": _pids}) or []
                        for _r in _inv_rows:
                            _sp_map[str(_r["product_id"])] = float(_r["selling_price"])

                        # Fill gaps from products table
                        _missing = [p for p in _pids if p not in _sp_map]
                        if _missing:
                            _prod_rows = _rqbatch("""
                                SELECT id::text, selling_price, unit_price
                                FROM products
                                WHERE id::text = ANY(%(pids)s)
                            """, {"pids": _missing}) or []
                            for _r in _prod_rows:
                                _v = float(_r.get("selling_price") or _r.get("unit_price") or 0)
                                if _v > 0:
                                    _sp_map[str(_r["id"])] = _v
                except Exception as _be:
                    logger.warning(f"[BO] Batch price lookup failed: {_be}")

            # tax_inclusive derived from order_type — NOT from line field (not persisted in DB)
            _tax_inc_e = (_otype_e == "RETAIL")

            for _el in _all_order_lines2:
                # Always stamp tax_inclusive so display functions are consistent
                _el["tax_inclusive"] = _tax_inc_e

                # ── PRICE RULE (governor) ─────────────────────────────────
                # RETAIL    → mrp        (GST-inclusive, per PCS after /box_size)
                # WHOLESALE → selling_price (ex-GST, per BOX — divide for per-PCS)
                # PURCHASE  → purchase_rate (ex-GST, per BOX)
                #
                # IMPORTANT: we NEVER overwrite unit_price in the line dict.
                # unit_price = what was agreed at time of order (historical truth).
                # We only recompute billing_total + gst_amount for the display card.
                # If DB price changed after order, the order total stays unchanged.
                # ─────────────────────────────────────────────────────────

                _upr = float(_el.get("unit_price") or 0)

                # For WHOLESALE: annotate selling_price for display reference only.
                # Do NOT overwrite unit_price — preserve historical order price.
                if _otype_e == "WHOLESALE":
                    _pid_e = str(_el.get("product_id") or "")
                    _sp_e  = _sp_map.get(_pid_e, 0.0)
                    if _sp_e > 0:
                        _el["selling_price"] = _sp_e    # display reference only
                        # Flag if current DB price differs from stored order price
                        _bsz_e   = max(1, int(_el.get("box_size") or 1))
                        _sp_pcs  = round(_sp_e / _bsz_e, 2)
                        if _upr > 0 and abs(_sp_pcs - _upr) > 0.05:
                            _el["_price_drifted"] = True
                            _el["_current_sp_pcs"] = _sp_pcs

                if _upr <= 0:
                    continue  # no price — leave as-is

                # Recompute billing_total using normalize_box_total for BOX products
                _bq    = int(_el.get("billing_qty") or _el.get("quantity") or 0)
                _gpct  = float(_el.get("gst_percent_used") or _el.get("gst_percent") or 0)
                _bsz_c = max(1, int(_el.get("box_size") or 1))

                # Reconstruct BOX price from stored per-PCS price for exact total
                _box_price_for_total = round(_upr * _bsz_c, 2)
                try:
                    from modules.core.price_qty_governor import normalize_box_total as _nbt
                    _sub = _nbt(_box_price_for_total, _bq, _el)
                except Exception:
                    _sub = round(_bq * _upr, 2)

                if _tax_inc_e and _gpct:
                    # RETAIL: MRP includes GST — back-calculate
                    _gst = round(_sub * _gpct / (100 + _gpct), 2)
                    _el["billing_total"] = round(_sub - _gst, 2)
                    _el["gst_amount"]    = _gst
                else:
                    # WHOLESALE/PURCHASE: GST added on top
                    _gst = round(_sub * _gpct / 100, 2)
                    _el["billing_total"] = _sub
                    _el["gst_amount"]    = _gst

            # total_value = what customer pays (grand total incl GST) for dashboard card
            order["total_value"] = round(sum(
                float(_el.get("billing_total") or 0) + float(_el.get("gst_amount") or 0)
                for _el in _all_order_lines2
            ), 2)
            # ─────────────────────────────────────────────────────────────────

            result.append(order)

        # Bulk-fetch sequential display numbers (safe — no crash if column missing)
        _all_onos = [o.get("order_no") for o in result if o.get("order_no")]
        _disp_map = _fetch_display_numbers(_all_onos)
        for o in result:
            ono = o.get("order_no")
            if ono and ono in _disp_map:
                o["display_order_no"] = _disp_map[ono]

        logger.info(f"[BO] Loaded {len(result)} orders (BATCH MODE + Hydrated + GST stamped)")

        return result

    except Exception as e:
        logger.exception("[BO] Batch load failed")
        st.error("Backoffice load failed — check logs")
        return []


# Clear stale cache on module reload (e.g. after deploy)
try:
    load_orders_from_database.clear()
except Exception:
    pass


def categorize_order_lines(order: Dict) -> None:
    """
    Categorizes order lines into stock_lines / inhouse_lines / lab_order_lines.

    Priority:
      1. manufacturing_route field (set by workflow engine at punching time)
         STOCK        → stock_lines
         INHOUSE      → inhouse_lines
         VENDOR       → lab_order_lines  (supplier PO route)
         EXTERNAL_LAB → lab_order_lines
      2. batch_status fallback (for old orders that pre-date routing)
         ALLOCATED    → stock_lines
      3. main_group heuristic (last resort)
         ophthalmic / contact → inhouse_lines
         everything else      → lab_order_lines
    """
    stock_lines    = []
    inhouse_lines  = []
    lab_order_lines = []

    service_lines = []

    for line in order.get("lines", []):
        eye_side     = str(line.get("eye_side") or "").upper()
        route        = (line.get("manufacturing_route") or "").upper()
        batch_status = (line.get("batch_status") or "").upper()
        main_group   = (line.get("main_group") or "").lower()

        # ── SERVICE lines (consultation fee, eye testing) ────────────────
        # Never goes to production, allocation, or supplier routing.
        # Kept in a separate bucket and added to all_lines at save time.
        if eye_side in ("SERVICE", "S") or line.get("is_service_line"):
            service_lines.append(line)
            continue

        if route == "STOCK":
            stock_lines.append(line)
        elif route == "INHOUSE":
            inhouse_lines.append(line)
        elif route in ("VENDOR", "EXTERNAL_LAB"):
            lab_order_lines.append(line)
        # ── fallback: no route saved yet ─────────────────────────────────
        elif batch_status == "ALLOCATED":
            stock_lines.append(line)
        elif "ophthalmic" in main_group or "contact" in main_group:
            inhouse_lines.append(line)
        else:
            lab_order_lines.append(line)

    order["stock_lines"]     = stock_lines
    order["inhouse_lines"]   = inhouse_lines
    order["lab_order_lines"] = lab_order_lines
    order["service_lines"]   = service_lines  # consultation/eye-testing fees


# ====================================================================
# SYSTEM DIAGNOSTICS
# ====================================================================

def run_system_health_check(order):
    """
     DIAGNOSTIC: Validate order data consistency
    """
    
    issues = []
    
    # Check order lines exist
    all_lines = []
    all_lines.extend(order.get('stock_lines', []))
    all_lines.extend(order.get('inhouse_lines', []))
    all_lines.extend(order.get('lab_order_lines', []))
    
    if not all_lines:
        issues.append(" No order lines found")
    
    # Check each line
    for idx, line in enumerate(all_lines):
        
        # Check power values
        if line.get('sph') is None:
            issues.append(f"Line {idx}: Missing SPH value")
        
        if line.get('sph_out') is None:
            issues.append(f"Line {idx}: Missing manufacturing power (sph_out)")
        
        # Check allocation
        if line.get('batch_status') == 'ALLOCATED' and not line.get('batch_allocation'):
            issues.append(f"Line {idx}: Status is ALLOCATED but no batch_allocation")
        
        # Check billing
        if line.get('billing_qty', 0) > 0 and line.get('billing_total', 0) == 0:
            issues.append(f"Line {idx}: Quantity > 0 but billing_total = 0")

    # Stock allocation drift check (detect only — no fix during health check)
    try:
        from modules.backoffice.audit_logger import reconcile_stock_allocations
        _recon = reconcile_stock_allocations(fix=False)
        for _dr in (_recon.get("drifted") or []):
            issues.append(
                f"Stock drift: product {str(_dr.get('product_id',''))[:8]}… "
                f"batch={_dr.get('batch_no','?')} "
                f"drift={_dr.get('drift',0)} pcs"
            )
    except Exception:
        pass  # health check never blocks

    # Display results
    if issues:
        with st.expander(" System Health Check - Issues Found", expanded=True):
            for issue in issues:
                st.error(issue) if "billing_total = 0" in issue else st.warning(issue)
    else:
        st.success(" System Health Check: All OK")
