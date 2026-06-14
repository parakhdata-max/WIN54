from modules.core.order_engine import convert_cart_to_order
from modules.db.order_repository import save_order
from modules.validation_gateway import validate_before_submit
from modules.workflow.workflow_engine import WorkflowEngine
from modules.quantity_engine import QuantityEngine
from modules.core.finalize_engine import run_finalize
from modules.core.business_rules import INITIAL_ORDER_STATUS, PIPELINE_SUCCESS_STATUS
import copy


class OrderPipeline:

    def submit_retail(self, cart_lines, order_info, user_name):

        # -------------------------------------------------
        # 1. Build Order (Pure Engine)
        # -------------------------------------------------
        order = convert_cart_to_order(
            copy.deepcopy(cart_lines),
            order_info,
            forced_order_id=order_info.get("provisional_order_id") or None,
        )

        # -------------------------------------------------
        # 2. Finalize — schema + validate + price + audit
        # -------------------------------------------------
        #
        # IMPORTANT: run_finalize() expects (cart_lines, order_info, user_name)
        #   - cart_lines = order["lines"]   ← the list of line dicts
        #   - order_info = order            ← the order header dict
        #   - returns a plain dict, NOT an object
        #
        USE_FINALIZE = True

        if USE_FINALIZE:
            finalize_result = run_finalize(
                cart_lines = order["lines"],
                order_info = order,
                user_name  = user_name,
            )

            if finalize_result["status"] == "REJECTED":
                return {
                    "status":   "REJECTED",
                    "errors":   finalize_result["errors"],
                    "warnings": finalize_result["warnings"],
                }

            # Merge finalized (normalized + priced) lines back into order
            order["lines"] = finalize_result["lines"]

        else:
            # ---- OLD VALIDATION BLOCK (kept for rollback) ----
            validation = validate_before_submit({
                "order_type": order["order_type"],
                "party_name": order["party"],
                "lines":      order["lines"],
                "total_value":order["total_value"]
            })

            if not validation["is_valid"]:
                return {
                    "status":   "REJECTED",
                    "errors":   validation["errors"],
                    "warnings": validation["warnings"]
                }

        # -------------------------------------------------
        # 3. Save Order Header
        # -------------------------------------------------
        # Retail/Wholesale orders punched from the counter start as UNDER_REVIEW.
        # Backoffice staff review the order and explicitly move it to CONFIRMED.
        # CONFIRMED is only set after a human backoffice action — never on punch.
        _initial_status = INITIAL_ORDER_STATUS  # set in business_rules.py

        # ── Format proper order_no — set after we know display_no ──────
        # Will be updated below after next_order_number is called in save_order
        # For now use provisional — save_order will overwrite with formatted no
        _provisional_ono = order["order_id"]
        db_order_data = {
            "order_no":           _provisional_ono,   # overwritten by save_order
            "order_type":         order["order_type"],
            "status":             _initial_status,
            "party_name":         order["party"],
            "patient_name":       order["patient_name"],
            "patient_mobile":     order["patient_mobile"],
            "customer_order_no":  order["customer_order_no"],
            "total_items":        order["total_items"],
            "total_value":        order["total_value"],
            "party_id":           order.get("party_id") or None,
            "payment_mode":       order.get("payment_mode") or (
                                      "ADVANCE_BALANCE" if order["order_type"] == "RETAIL"
                                      else "ON_COMPLETION"
                                  ),
        }

        # -------------------------------------------------
        # 4. Prepare Order Lines for DB
        # -------------------------------------------------

        # ── Price Integrity Guard ────────────────────────────────────────────
        # Last-chance check before DB write. If any line has unit_price=0 but
        # has a non-zero total_price, back-calc. If billing_qty missing, use
        # requested_qty. Prevents silent price loss from any upstream gap.
        for l in order["lines"]:
            # billing_qty guard
            if not l.get("billing_qty") and l.get("requested_qty"):
                l["billing_qty"] = l["requested_qty"]
            # unit_price back-calc
            qty = int(l.get("billing_qty") or 0)
            if l.get("unit_price", 0) == 0 and l.get("total_price", 0) > 0 and qty > 0:
                l["unit_price"] = round(float(l["total_price"]) / qty, 2)
            # total_price back-calc
            if l.get("total_price", 0) == 0 and l.get("unit_price", 0) > 0 and qty > 0:
                try:
                    from modules.core.price_qty_governor import compute_line_gst as _clg_p
                    _ot_p  = str(order.get("order_type") or "RETAIL").upper()
                    _gst_p2= float(l.get("gst_percent") or 0)
                    l["total_price"] = _clg_p(float(l["unit_price"]), qty, _gst_p2, _ot_p)["grand_total"]
                except Exception:
                    l["total_price"] = round(float(l["unit_price"]) * qty, 2)
            # gst_amount back-calc if missing
            if l.get("gst_amount", 0) == 0 and l.get("total_price", 0) > 0:
                gst_pc = float(l.get("gst_percent") or 0)
                if gst_pc > 0:
                    total = float(l["total_price"])
                    order_type = order.get("order_type", "RETAIL").upper()
                    if order_type == "RETAIL":
                        l["gst_amount"] = round(total - total / (1 + gst_pc / 100), 2)
                    else:
                        l["gst_amount"] = round(total * gst_pc / 100, 2)
        # ────────────────────────────────────────────────────────────────────

        db_lines = []

        # ── Apply discount rules BEFORE building db_lines ─────────────────────
        # Full cart lines (with brand, main_group) available here.
        # Party UUID resolved from party_name (wholesale doesn't pass it).
        try:
            from modules.pricing.discount_engine import apply_discounts
            _ot  = str(order.get("order_type") or "wholesale")
            _pid = str(order.get("party_id") or "").strip()
            if not _pid:
                _pname = str(order.get("party") or order.get("party_name") or "").strip()
                if _pname:
                    try:
                        from modules.sql_adapter import run_query
                        _rows = run_query(
                            "SELECT id::text AS id FROM parties "
                            "WHERE party_name = %s "
                            "AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                            (_pname,)
                        ) or []
                        if _rows: _pid = str(_rows[0].get("id") or "")
                    except Exception: pass
            apply_discounts(order["lines"], party_id=_pid, order_type=_ot)
            # Club offers — cart-level, after apply_discounts
            try:
                from modules.pricing.club_engine import apply_club_offers
                apply_club_offers(order["lines"], order_type=_ot)
            except Exception: pass
            # Supplier/own-product schemes are per-line; cart schemes are
            # multi-line offers like 1+1 or CL 12+2. Run them before GST is
            # stamped so tax reflects the final scheme-adjusted amount.
            try:
                from modules.pricing.supplier_scheme_engine import apply_customer_scheme_to_line
                order["lines"] = [
                    apply_customer_scheme_to_line(_ol, party_id=_pid, order_type=_ot)
                    for _ol in order["lines"]
                ]
            except Exception: pass
            try:
                from modules.pricing.cart_scheme_engine import apply_cart_schemes
                order["lines"], _cart_result = apply_cart_schemes(
                    order["lines"], party_id=_pid, order_type=_ot
                )
                if getattr(_cart_result, "applied", False):
                    order.setdefault("pricing_audit", {})["cart_scheme"] = getattr(_cart_result, "message", "")
            except Exception: pass
            # Re-stamp GST on net price after discount
            try:
                from modules.pricing.tax_engine import apply_taxes as _at
                for _ol in order["lines"]:
                    _d2 = float(_ol.get("discount_amount") or 0)
                    if _d2 > 0:
                        _ol["billing_total"] = round(
                            float(_ol.get("total_price") or 0) - _d2, 2)
                _at({"order_type": _ot, "lines": order["lines"],
                     "net_value": sum(float(l2.get("billing_total") or
                                      l2.get("total_price") or 0)
                                     for l2 in order["lines"])})
            except Exception: pass
        except Exception as _de:
            import logging as _dl
            _dl.getLogger(__name__).warning(f"[OrderPipeline] discount skipped: {_de}")

        for l in order["lines"]:
            # billing_total = net price after discount (what backoffice + challan bill on)
            _disc_amt = float(l.get("discount_amount") or 0)
            _gross    = float(l.get("total_price") or 0)
            _net      = round(_gross - _disc_amt, 2) if _disc_amt > 0 else _gross
            if _net < 0:
                _net = 0.0

            _gst_pct = float(l.get("gst_percent") or 0)
            _gst_amt = float(l.get("gst_amount") or 0)
            _lp_svc = l.get("lens_params") if isinstance(l.get("lens_params"), dict) else {}
            _is_svc = (
                bool(l.get("is_service_line"))
                or str(l.get("eye_side") or "").upper() in ("S", "SERVICE")
                or bool(_lp_svc.get("service_type") or _lp_svc.get("charge_type"))
            )
            _order_type_line = str(order.get("order_type") or l.get("order_type") or "WHOLESALE").upper()
            _billing_total = (
                round(_net + _gst_amt, 2)
                if _is_svc and _order_type_line != "RETAIL" and _gst_pct > 0
                else _net
            )

            db_lines.append({
                "product_id":           l.get("product_id"),
                "sph":                  l.get("sph"),
                "cyl":                  l.get("cyl"),
                "axis":                 l.get("axis"),
                "add_power":            l.get("add_power"),
                "eye_side":             l.get("eye_side"),
                "quantity":             l.get("billing_qty"),
                "unit_price":           l.get("unit_price"),
                # total_price persisted as NET so reports/accounting that read
                # this column directly (without going through the loader's
                # billing_total alias) see the post-discount value.
                # Mirrors retail_punching after restamp_line_totals.
                "total_price":          _net,
                "billing_total":        _billing_total,
                "gst_percent":          l.get("gst_percent", 0),
                "gst_amount":           l.get("gst_amount", 0),
                "discount_percent":     l.get("discount_percent", 0),
                "discount_amount":      l.get("discount_amount", 0),
                "lens_params":          l.get("lens_params", {}),
                "boxing_params":        l.get("boxing_params", {}),
                "suggested_allocation": l.get("batch_allocation", []),
            })

        # ── Recompute order header total_value AS NET ──────────────────────
        # order_engine.convert_cart_to_order ran BEFORE apply_discounts above,
        # so order["total_value"] there is sum(total_price) at that moment —
        # gross when discounts hadn't been applied yet. Now that discounts
        # are stamped and net is known, re-roll the header total so
        # orders.total_value lands as net in the DB.
        try:
            _net_total = sum(float(_d.get("billing_total") or 0) for _d in db_lines)
            order["total_value"]         = round(_net_total, 2)
            db_order_data["total_value"] = round(_net_total, 2)
        except Exception:
            pass

        # -------------------------------------------------
        # 5. Save to Database
        # -------------------------------------------------
        order_id = save_order(
            order_data = db_order_data,
            lines      = db_lines,
            user_name  = user_name
        )

        # -------------------------------------------------
        # 6. Initialize Workflow
        # -------------------------------------------------
        workflow_engine = WorkflowEngine()

        for line in order["lines"]:
            route = self._detect_route(line)
            workflow_engine.initialize_line(line, route)

        # -------------------------------------------------
        # 7. Return Success
        # -------------------------------------------------
        # save_order() now returns order_no already formatted and committed
        # in ONE atomic transaction. No second UPDATE needed here.
        if isinstance(order_id, dict):
            _uuid_str      = str(order_id.get("order_db_id") or "")
            _disp_no       = order_id.get("display_order_no", 0)
            _formatted_ono = order_id.get("order_no") or _provisional_ono
        else:
            _uuid_str      = str(order_id)
            _disp_no       = 0
            _formatted_ono = _provisional_ono

        # ── REMOVED: the old post-commit run_write("UPDATE orders SET order_no=...")
        # That was the root cause of missing order numbers:
        #   save_order() committed → number consumed
        #   second UPDATE failed   → order_no stayed as UUID
        #   UI couldn't find it    → appeared "missing"
        # Now order_no is formatted and inserted inside save_order()'s single commit.

        return {
            "status":           PIPELINE_SUCCESS_STATUS,
            "order_id":         _uuid_str,
            "order_no":         _formatted_ono,
            "display_order_no": _disp_no,
        }


    def _detect_route(self, line):

        if line.get("billing_qty", 0) > 0:
            return "STOCK"

        if line.get("order_qty", 0) > 0:
            return "EXTERNAL"

        return "INHOUSE"
