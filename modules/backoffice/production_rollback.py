"""
production_rollback.py
======================
Shared rollback logic for all production pipelines:
  - In-house  (inhouse_pipeline.py)
  - Supplier  (supplier_pipeline.py, route_filter="VENDOR")
  - External  (supplier_pipeline.py, route_filter="EXTERNAL_LAB")

USAGE — import both symbols in any pipeline file:

    from modules.backoffice.production_rollback import (
        rollback_order_to_backoffice,
        render_set_back_panel,
    )
"""

from __future__ import annotations

import json as _json
import logging as _logging
import traceback as _tb
from typing import Dict, List, Optional

import streamlit as st

_log = _logging.getLogger(__name__)


# ============================================================================
# CORE ROLLBACK ENGINE
# ============================================================================

def rollback_order_to_backoffice(
    order_id_uuid: str,
    order_no: str,
    operator: str = "Production",
    route_label: str = "Production",
    extra_clears: Optional[List[str]] = None,
) -> tuple:
    """
    Cancel all open job cards and reset the order to CONFIRMED.
    Returns (ok: bool, message: str).
    """
    _extra = list(extra_clears or [])

    _always_clear = [
        "surfacing_data", "blank_id", "manufacturing_route", "batch_allocation",
        "supplier_stage", "external_lab_stage", "po_number", "dispatch_eta",
        "purchase_order_id", "vendor_order_ref", "supplier_order_id",
        "lab_order_ref", "supplier_dispatch_date",
    ]
    _keys_to_clear = list(dict.fromkeys(_always_clear + _extra))

    try:
        from modules.sql_adapter import run_query as _rq, run_write as _rw

        # ── Resolve the real UUID from order_no — single source of truth ──
        # Any composite key format (uuid:order_no) or wrong UUID from the
        # caller is bypassed. order_no is always correct.
        _resolved = None
        if order_no and order_no.strip():
            try:
                _res_rows = _rq(
                    "SELECT id::text AS oid, status FROM orders "
                    "WHERE order_no = %(ono)s LIMIT 1",
                    {"ono": order_no.strip()},
                )
                if _res_rows:
                    _resolved = _res_rows[0]
            except Exception as _re:
                _log.warning("[rollback] order_no lookup failed: %s", _re)

        if not _resolved:
            # Fallback: try the passed UUID (strip composite key format)
            _clean_uuid = str(order_id_uuid or "").split(":")[0].strip()
            if len(_clean_uuid) >= 32:
                try:
                    _res_rows2 = _rq(
                        "SELECT id::text AS oid, status FROM orders "
                        "WHERE id = %(oid)s::uuid LIMIT 1",
                        {"oid": _clean_uuid},
                    )
                    if _res_rows2:
                        _resolved = _res_rows2[0]
                except Exception:
                    pass

        if not _resolved:
            _log.error(
                "[rollback] order not found — order_no=%r uuid=%r",
                order_no, order_id_uuid,
            )
            return False, (
                f"❌ Order '{order_no}' not found in database. "
                "Check the order number and try again."
            )

        _real_uuid   = str(_resolved["oid"])
        _from_status = str(_resolved.get("status") or "IN_PRODUCTION")

        _log.info(
            "[rollback] resolved order_no=%s → uuid=%s status=%s",
            order_no, _real_uuid[:8], _from_status,
        )

        # ── 2. Remove job_master rows for this order ─────────────────────
        # We DELETE rather than set CANCELLED because:
        #   - CANCELLED stage is not in inhouse_pipeline._active_stages, which
        #     causes the order to be filtered out of the pipeline list entirely.
        #   - When the order re-enters production, fresh job_master rows are
        #     created by the job card assignment flow.
        #   - The prevent_hard_delete trigger only covers order_lines, not job_master.
        # If DELETE is blocked by a trigger, fall back to CANCELLED + is_closed.
        try:
            # Write audit event first (before rows disappear)
            _rw(
                """
                INSERT INTO job_stage_events
                    (job_id, stage_code, performed_by, remarks, created_at)
                SELECT jm.id, 'CANCELLED', %(op)s,
                       %(route)s || ' — set back to backoffice by ' || %(op)s, NOW()
                FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(jm.is_closed, FALSE) = FALSE
                """,
                {"oid": _real_uuid, "op": operator, "route": route_label},
            )
        except Exception as _e1a:
            _log.debug("[rollback:%s] job_stage_events insert skipped: %s", order_no, _e1a)

        try:
            _rw(
                """
                DELETE FROM job_master
                WHERE order_line_id IN (
                    SELECT id FROM order_lines
                    WHERE order_id = %(oid)s::uuid
                      AND COALESCE(is_deleted, FALSE) = FALSE
                )
                """,
                {"oid": _real_uuid},
            )
            _log.info("[rollback:%s] job_master rows deleted", order_no)
        except Exception as _e1b:
            # DELETE blocked — fall back to CANCELLED + closed flag
            _log.warning(
                "[rollback:%s] job_master DELETE blocked (%s), falling back to CANCELLED",
                order_no, _e1b,
            )
            try:
                _rw(
                    """
                    UPDATE job_master
                    SET current_stage = 'CANCELLED',
                        is_closed     = TRUE,
                        updated_at    = NOW()
                    WHERE order_line_id IN (
                        SELECT id FROM order_lines
                        WHERE order_id = %(oid)s::uuid
                          AND COALESCE(is_deleted, FALSE) = FALSE
                    )
                      AND COALESCE(is_closed, FALSE) = FALSE
                    """,
                    {"oid": _real_uuid},
                )
            except Exception as _e1c:
                _log.warning("[rollback:%s] job_master cancel also failed: %s", order_no, _e1c)

        # ── 2b. Restore blank_inventory qty + clear blank_allocations ───
        # blank_allocations tracks surfacing blank reservations.
        # When a job card is printed, blank_inventory.qty_right/qty_left is
        # decremented. On rollback we must restore that qty before deleting
        # the allocation record, otherwise the blank is lost from inventory.
        try:
            # Find all allocations for this order with their eye_side
            _ba_rows = _rq(
                """
                SELECT ba.id::text AS ba_id,
                       ba.blank_id::text AS bid,
                       ba.eye_side
                FROM blank_allocations ba
                JOIN order_lines ol ON ol.id = ba.order_line_id
                WHERE ol.order_id = %(oid)s::uuid
                  AND COALESCE(ol.is_deleted, FALSE) = FALSE
                """,
                {"oid": _real_uuid},
            ) or []

            for _ba in _ba_rows:
                _bid  = str(_ba.get("bid") or "")
                _eye  = str(_ba.get("eye_side") or "").upper().strip()
                if not _bid:
                    continue
                # Restore the correct qty column based on eye_side
                if _eye in ("R", "RIGHT"):
                    _qty_col = "qty_right"
                elif _eye in ("L", "LEFT"):
                    _qty_col = "qty_left"
                else:
                    _qty_col = "qty_independent"
                try:
                    _rw(
                        f"""
                        UPDATE blank_inventory
                        SET {_qty_col} = COALESCE({_qty_col}, 0) + 1,
                            updated_at = NOW()
                        WHERE id = %(bid)s::uuid
                        """,
                        {"bid": _bid},
                    )
                    _log.info(
                        "[rollback:%s] blank_inventory %s restored for eye=%s blank=%s",
                        order_no, _qty_col, _eye, _bid[:8],
                    )
                except Exception as _bi_err:
                    _log.warning(
                        "[rollback:%s] blank_inventory restore failed (eye=%s blank=%s): %s",
                        order_no, _eye, _bid[:8], _bi_err,
                    )

            # Write ledger entries for the restorations
            for _ba in _ba_rows:
                _bid  = str(_ba.get("bid") or "")
                _eye  = str(_ba.get("eye_side") or "").upper().strip()
                _ba_id = str(_ba.get("ba_id") or "")
                if not _bid:
                    continue
                try:
                    _rw(
                        """
                        INSERT INTO blank_stock_ledger
                            (blank_id, order_line_id, eye_side, qty_change,
                             ref_type, ref_id, remarks, created_at, created_by)
                        SELECT
                            %(bid)s::uuid,
                            ol.id,
                            %(eye)s,
                            +1,
                            'ROLLBACK',
                            %(oid)s::uuid,
                            %(rmk)s,
                            NOW(),
                            %(op)s
                        FROM order_lines ol
                        WHERE ol.order_id = %(oid)s::uuid
                          AND UPPER(COALESCE(ol.eye_side,'')) = %(eye)s
                          AND COALESCE(ol.is_deleted, FALSE) = FALSE
                        LIMIT 1
                        """,
                        {
                            "bid": _bid,
                            "eye": _eye,
                            "oid": _real_uuid,
                            "rmk": f"Blank restored — order {order_no} rolled back from {route_label}",
                            "op":  operator,
                        },
                    )
                except Exception as _led_err:
                    _log.debug(
                        "[rollback:%s] ledger write skipped (eye=%s): %s",
                        order_no, _eye, _led_err,
                    )

            # Now delete the allocation records
            _rw(
                """
                DELETE FROM blank_allocations
                WHERE order_line_id IN (
                    SELECT id FROM order_lines
                    WHERE order_id = %(oid)s::uuid
                      AND COALESCE(is_deleted, FALSE) = FALSE
                )
                """,
                {"oid": _real_uuid},
            )
            _log.info(
                "[rollback:%s] blank_allocations cleared (%d rows)", order_no, len(_ba_rows)
            )
        except Exception as _e1c:
            _log.warning(
                "[rollback:%s] blank_allocations/inventory restore failed: %s", order_no, _e1c
            )

        # ── 3. Clear production keys from lens_params ─────────────────────
        try:
            _lines = _rq(
                "SELECT id::text AS lid, COALESCE(lens_params,'{}')::text AS lp "
                "FROM order_lines WHERE order_id = %(oid)s::uuid "
                "AND COALESCE(is_deleted,FALSE)=FALSE",
                {"oid": _real_uuid},
            ) or []
            for _lr in _lines:
                try:
                    _lp = _json.loads(_lr["lp"] or "{}") or {}
                except Exception:
                    _lp = {}
                for _k in _keys_to_clear:
                    _lp.pop(_k, None)
                _lp["batch_status"] = "PENDING"
                _lp["batch_allocation"] = []
                _rw(
                    "UPDATE order_lines SET lens_params = %(lp)s::jsonb "
                    "WHERE id = %(lid)s::uuid",
                    {"lp": _json.dumps(_lp), "lid": _lr["lid"]},
                )
        except Exception as _e2:
            _log.warning("[rollback:%s] lens_params clear failed: %s", order_no, _e2)

        # ── 4. Reset line allocation fields ───────────────────────────────
        try:
            _rw(
                """
                UPDATE order_lines
                SET allocated_qty        = 0,
                    batch_status         = 'PENDING',
                    suggested_allocation = NULL
                WHERE order_id = %(oid)s::uuid
                  AND COALESCE(is_deleted, FALSE) = FALSE
                """,
                {"oid": _real_uuid},
            )
        except Exception as _e3:
            _log.warning("[rollback:%s] line reset failed: %s", order_no, _e3)

        # ── 5. Reset order status — verify rowcount ───────────────────────
        # This is the critical step. We verify the row was actually updated.
        _verify_before = _rq(
            "SELECT status FROM orders WHERE id = %(oid)s::uuid LIMIT 1",
            {"oid": _real_uuid},
        ) or []
        _status_before = str((_verify_before[0].get("status") if _verify_before else "") or "")

        _rw(
            "UPDATE orders SET status = 'CONFIRMED', updated_at = NOW() "
            "WHERE id = %(oid)s::uuid",
            {"oid": _real_uuid},
        )

        _verify_after = _rq(
            "SELECT status FROM orders WHERE id = %(oid)s::uuid LIMIT 1",
            {"oid": _real_uuid},
        ) or []
        _status_after = str((_verify_after[0].get("status") if _verify_after else "") or "")

        if _status_after != "CONFIRMED":
            _log.error(
                "[rollback:%s] status update FAILED — before=%s after=%s uuid=%s",
                order_no, _status_before, _status_after, _real_uuid,
            )
            return False, (
                f"❌ Rollback failed: order status is '{_status_after}' after update "
                f"(expected CONFIRMED). UUID used: {_real_uuid[:8]}... "
                "Check DB permissions or triggers blocking the status change."
            )

        _log.info(
            "[rollback:%s] status updated %s → CONFIRMED", order_no, _status_before
        )

        # ── 6. Status history ─────────────────────────────────────────────
        try:
            _rw(
                """
                INSERT INTO order_status_history
                    (order_id, from_status, to_status, changed_at, changed_by_name, remarks)
                VALUES (
                    %(oid)s::uuid, %(frm)s, 'CONFIRMED',
                    NOW(), %(op)s, %(rmk)s
                )
                """,
                {
                    "oid": _real_uuid,
                    "frm": _from_status,
                    "op":  operator,
                    "rmk": (
                        f"Set back to Backoffice from {route_label} pipeline — "
                        "job cards cancelled, lines reset"
                    ),
                },
            )
        except Exception as _e5:
            _log.warning("[rollback:%s] status_history write failed: %s", order_no, _e5)

        # ── 7. Audit log ──────────────────────────────────────────────────
        try:
            from modules.backoffice.audit_logger import audit, AuditAction
            audit(
                AuditAction.STATUS_CHANGED,
                entity="orders",
                entity_id=_real_uuid,
                order_id=order_no,
                payload={
                    "action": "rollback_to_backoffice",
                    "pipeline": route_label,
                    "from_status": _from_status,
                    "to_status": "CONFIRMED",
                    "order_no": order_no,
                    "cancelled_by": operator,
                },
            )
        except Exception as _e6:
            _log.debug("[rollback:%s] audit write skipped: %s", order_no, _e6)

        # ── 8. Cache busting ──────────────────────────────────────────────
        try:
            from modules.backoffice.backoffice_helpers import load_orders_from_database
            load_orders_from_database.clear()
        except Exception:
            pass
        try:
            for _k in list(st.session_state.keys()):
                if _k.startswith((
                    "_bo_challan_exists_",
                    "bo_",
                    "_prod_",
                    "_bo_psa_just_saved_",
                )):
                    st.session_state.pop(_k, None)
        except Exception:
            pass

        return True, (
            f"✅ Order **{order_no}** returned to Backoffice.\n\n"
            f"Route: {route_label} · Job cards cancelled · Lines reset · "
            f"Status: CONFIRMED — editable in Backoffice."
        )

    except Exception as _top:
        _log.error("[rollback:%s] fatal: %s\n%s", order_no, _top, _tb.format_exc())
        return False, f"Rollback failed: {_top}"


# ============================================================================
# SHARED UI PANEL
# ============================================================================

_ROUTE_EXTRA_CLEARS: Dict[str, List[str]] = {
    "In-house": [],
    "Supplier": [
        "supplier_stage", "po_number", "dispatch_eta",
        "purchase_order_id", "vendor_order_ref", "supplier_order_id",
        "supplier_dispatch_date",
    ],
    "External Lab": [
        "external_lab_stage", "lab_order_ref", "po_number",
        "dispatch_eta", "purchase_order_id", "supplier_dispatch_date",
    ],
}

_ROUTE_BULLETS: Dict[str, str] = {
    "In-house": (
        "- Cancel all job cards (Job Created / In Progress)\n"
        "- Clear blank allocations and surfacing data\n"
        "- Reset order lines to PENDING allocation\n"
    ),
    "Supplier": (
        "- Cancel any open supplier job records\n"
        "- Clear PO number, dispatch ETA, and supplier stage\n"
        "- Reset order lines to PENDING allocation\n"
    ),
    "External Lab": (
        "- Cancel any open external lab job records\n"
        "- Clear lab order ref, external stage, and dispatch ETA\n"
        "- Reset order lines to PENDING allocation\n"
    ),
}


def render_set_back_panel(
    order: Dict,
    route_label: str = "In-house",
) -> None:
    """
    Render the ↩ Set Back to Backoffice panel for any pipeline.
    order dict must have: id/order_id (UUID), order_no, status.
    """
    _oid  = str(order.get("id") or order.get("order_id") or "")
    _ono  = str(order.get("order_no") or "")
    _stat = str(order.get("status") or "").upper()

    _eligible_statuses = {
        "IN_PRODUCTION", "CONFIRMED", "READY",
        "READY_TO_BILL", "READY_FOR_BILLING", "JOB_CREATED",
        "ORDER_PLACED", "DISPATCHED_BY_SUPPLIER", "RECEIVED_FROM_SUPPLIER",
        "SENT_TO_LAB", "LAB_IN_PROGRESS", "LAB_READY",
    }
    if _stat not in _eligible_statuses or not _ono:
        return

    # Block if challan exists
    try:
        from modules.sql_adapter import run_query as _rq_ch
        _ch = _rq_ch(
            "SELECT 1 FROM challans "
            "WHERE order_ids::text[] @> ARRAY[%(oid)s::text] "
            "AND status NOT IN ('CANCELLED','VOID') LIMIT 1",
            {"oid": _oid},
        )
        if _ch:
            st.info(
                "⚠️ A challan exists for this order. Cancel the challan from "
                "Billing before rolling back to Backoffice.",
                icon="🔒",
            )
            return
    except Exception:
        pass

    if route_label.lower().startswith("in-house"):
        try:
            from modules.sql_adapter import run_query as _rq_pd
            _prod_done = _rq_pd("""
                SELECT 1
                FROM job_master jm
                JOIN order_lines ol ON ol.id = jm.order_line_id
                LEFT JOIN job_stage_events jse ON jse.job_id = jm.id
                WHERE ol.order_id = %(oid)s::uuid
                  AND (
                        UPPER(COALESCE(jm.current_stage,'')) IN (
                            'PRODUCTION_DONE','INSPECTION','COLOURING_PICKED','COLOURING_DONE',
                            'HARDCOAT_PICKED','HARDCOAT_DONE','ARC_SENT','ARC_RECEIVED',
                            'FINAL_QC','READY_FOR_PACK','READY_TO_BILL','BILLED'
                        )
                     OR UPPER(COALESCE(jse.stage_code,'')) = 'PRODUCTION_DONE'
                  )
                LIMIT 1
            """, {"oid": _oid}) or []
            if _prod_done:
                st.info(
                    "🔒 Production has already crossed Production Done. "
                    "Use the Reject flow to restart the affected R/L lens; "
                    "Backoffice rollback is locked to protect stock and audit trail.",
                    icon="🔒",
                )
                return
        except Exception as _pd_e:
            _log.warning("[rollback] production-done guard failed: %s", _pd_e)
            st.info("🔒 Could not verify production stage. Refresh before rolling back.", icon="🔒")
            return

    _extra   = _ROUTE_EXTRA_CLEARS.get(route_label, [])
    _bullets = _ROUTE_BULLETS.get(route_label, _ROUTE_BULLETS["In-house"])
    _confirm_key = f"_sb_confirm_{_ono}_{route_label}"

    st.markdown(
        "<div style='background:#1a0700;border:2px solid #f97316;"
        "border-radius:10px;padding:12px 16px;margin:10px 0'>"
        "<div style='color:#fb923c;font-weight:700;font-size:0.88rem'>"
        f"↩ Set Back to Backoffice — {route_label}</div>"
        "<div style='color:#fed7aa;font-size:0.78rem;margin-top:4px'>"
        "Returns this order to Backoffice for re-editing. "
        f"All {route_label.lower()} pipeline state is cleared. "
        "Use this when the order was punched incorrectly or needs correction."
        "</div></div>",
        unsafe_allow_html=True,
    )

    if not st.session_state.get(_confirm_key):
        if st.button(
            "↩ Set Back to Backoffice",
            key=f"sb_btn_{_ono}_{route_label}",
            type="secondary",
            use_container_width=True,
        ):
            st.session_state[_confirm_key] = True
            st.rerun()
    else:
        st.warning(
            f"⚠️ **Confirm rollback for {_ono}?**\n\n"
            f"{_bullets}"
            "- Set order status → **CONFIRMED** (editable in Backoffice)\n\n"
            "This cannot be undone automatically."
        )
        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button(
                "✅ Yes — Return to Backoffice",
                key=f"sb_yes_{_ono}_{route_label}",
                type="primary",
                use_container_width=True,
            ):
                with st.spinner(f"Rolling back {route_label} order…"):
                    _ok, _msg = rollback_order_to_backoffice(
                        _oid, _ono,
                        operator="Production",
                        route_label=route_label,
                        extra_clears=_extra,
                    )
                st.session_state.pop(_confirm_key, None)
                if _ok:
                    st.success(_msg)
                    # ── If blanks were restored, offer quick jump to Blank Replenishment ──
                    try:
                        from modules.sql_adapter import run_query as _rq_ba_nav
                        _had_blanks = _rq_ba_nav(
                            """SELECT 1 FROM blank_stock_ledger bsl
                               JOIN orders o ON o.id = bsl.ref_id
                               WHERE o.order_no = %(ono)s
                                 AND bsl.ref_type = 'ROLLBACK'
                                 AND bsl.qty_change > 0
                               LIMIT 1""",
                            {"ono": _ono},
                        )
                        if _had_blanks:
                            st.info(
                                "🧫 **Blanks returned to stock.** "
                                "Check consumption and reorder if needed.",
                                icon="♻️",
                            )
                            if st.button(
                                "🧫 Open Blank Replenishment",
                                key=f"sb_goto_repl_{_ono}",
                                type="secondary",
                                use_container_width=True,
                            ):
                                # Use _prod_lazy_panel_next so production_page
                                # picks it up on the next render cycle
                                st.session_state["_prod_lazy_panel_next"] = "🧫 Blank Repl."
                                st.rerun()
                    except Exception:
                        pass
                    try:
                        for _nav_key in (
                            "prod_view_mode", "prod_selected_order",
                            "prod_orders_loaded", "prod_assign_order_no",
                            "supplier_selected_order", "ext_selected_order",
                        ):
                            if _nav_key == "prod_view_mode":
                                st.session_state[_nav_key] = "list"
                            elif _nav_key == "prod_orders_loaded":
                                st.session_state[_nav_key] = False
                            else:
                                st.session_state[_nav_key] = None
                    except Exception:
                        pass
                    st.rerun()
                else:
                    st.error(_msg)
        with _c2:
            if st.button(
                "✕ Cancel",
                key=f"sb_no_{_ono}_{route_label}",
                use_container_width=True,
            ):
                st.session_state.pop(_confirm_key, None)
                st.rerun()
