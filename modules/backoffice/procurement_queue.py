"""
modules/backoffice/procurement_queue.py
=========================================
📥 Procurement Queue — receive / match / save purchase acknowledgements.

Extracted from production_page.py (was too large).
Entry points:
  render_procurement_queue()      — main tab UI
  render_procurement_analytics()  — analytics sub-tab

Called from production_page.py:
  elif _view == "📥 Procurement Queue":
      from modules.backoffice.procurement_queue import render_procurement_queue
      render_procurement_queue()
"""
from __future__ import annotations
import datetime as _dt
import base64
import json
from collections import defaultdict
import streamlit as st


def _q2(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []


def _w2(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {}); return True
    except Exception as e:
        st.error(f"Write: {e}"); return False


def _auto_promote_po_sent_siblings() -> int:
    """
    If one eye on a stock replenishment PO was confirmed to ORDERED but its
    sibling eye stayed at PO_SENT, promote the sibling before the queue loads.
    This repairs the common one-click-missed R/L case without requiring staff
    to revisit Stock Replenishment and press Save Ref again.
    """
    try:
        from modules.sql_adapter import run_query, run_write
        rows = run_query("""
            WITH confirmed_po AS (
                SELECT
                    ol.lens_params->>'replenishment_po_no' AS po_no,
                    MAX(NULLIF(ol.lens_params->>'supplier_confirmation_no','')) AS supplier_ref,
                    MAX(NULLIF(ol.lens_params->>'replenishment_supplier_id','')) AS supplier_id,
                    MAX(NULLIF(ol.lens_params->>'replenishment_supplier_name','')) AS supplier_name
                FROM order_lines ol
                WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.lens_params->>'replenishment_status','')) = 'ORDERED'
                  AND COALESCE(ol.lens_params->>'replenishment_po_no','') <> ''
                  AND COALESCE(ol.lens_params->>'supplier_confirmation_no','') <> ''
                GROUP BY ol.lens_params->>'replenishment_po_no'
            )
            SELECT COUNT(*) AS n
            FROM order_lines ol
            JOIN confirmed_po cp
              ON cp.po_no = ol.lens_params->>'replenishment_po_no'
            WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
              AND UPPER(COALESCE(ol.lens_params->>'replenishment_status','')) = 'PO_SENT'
        """, {}) or []
        count = int((rows[0].get("n") if rows else 0) or 0)
        if count <= 0:
            return 0
        run_write("""
            WITH confirmed_po AS (
                SELECT
                    ol.lens_params->>'replenishment_po_no' AS po_no,
                    MAX(NULLIF(ol.lens_params->>'supplier_confirmation_no','')) AS supplier_ref,
                    MAX(NULLIF(ol.lens_params->>'replenishment_supplier_id','')) AS supplier_id,
                    MAX(NULLIF(ol.lens_params->>'replenishment_supplier_name','')) AS supplier_name
                FROM order_lines ol
                WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
                  AND UPPER(COALESCE(ol.lens_params->>'replenishment_status','')) = 'ORDERED'
                  AND COALESCE(ol.lens_params->>'replenishment_po_no','') <> ''
                  AND COALESCE(ol.lens_params->>'supplier_confirmation_no','') <> ''
                GROUP BY ol.lens_params->>'replenishment_po_no'
            )
            UPDATE order_lines ol
               SET lens_params =
                   COALESCE(ol.lens_params, '{}'::jsonb)
                   || jsonb_build_object(
                        'replenishment_status', 'ORDERED',
                        'supplier_confirmation_no', cp.supplier_ref,
                        'replenishment_ref_note', cp.supplier_ref,
                        'replenishment_supplier_id', COALESCE(cp.supplier_id, ol.lens_params->>'replenishment_supplier_id', ''),
                        'supplier_id', COALESCE(cp.supplier_id, ol.lens_params->>'supplier_id', ''),
                        'replenishment_supplier_name', COALESCE(cp.supplier_name, ol.lens_params->>'replenishment_supplier_name', ''),
                        'supplier_name', COALESCE(cp.supplier_name, ol.lens_params->>'supplier_name', '')
                   )
              FROM confirmed_po cp
             WHERE COALESCE(ol.is_deleted, FALSE) = FALSE
               AND ol.lens_params->>'replenishment_po_no' = cp.po_no
               AND UPPER(COALESCE(ol.lens_params->>'replenishment_status','')) = 'PO_SENT'
        """, {})
        for k in list(st.session_state.keys()):
            if str(k).startswith("_prx_rows_"):
                st.session_state.pop(k, None)
        return count
    except Exception as exc:
        st.caption(f"PO sibling auto-sync skipped: {exc}")
        return 0




def _render_order_db_check() -> None:
    """
    DB Diagnostic — check any order, view its stored procurement as a receipt,
    fix missing lens_params stage, and amend prices for GST paise corrections.
    """
    import decimal as _dec
    import html as _hesc

    def _r2(v):
        try:
            return float(_dec.Decimal(str(v)).quantize(_dec.Decimal("0.01"), rounding=_dec.ROUND_HALF_UP))
        except Exception:
            return round(float(v or 0), 2)

    with st.expander("🔍 DB Check — Order Procurement Status & Receipt", expanded=False):
        _dc1, _dc2 = st.columns([4, 1])
        _chk_ono = _dc1.text_input(
            "Order No to check", placeholder="e.g. R/2627/0120",
            key="dbchk_ono", label_visibility="collapsed"
        )
        _do_check = _dc2.button("Check DB", key="dbchk_btn", use_container_width=True)

        if not (_do_check and _chk_ono.strip()):
            return

        _ono = _chk_ono.strip()

        # ── 1. Fetch PA rows (the actual stored purchase record) ──────────
        _pa_rows = _q2("""
            SELECT
                pa.id::text                  AS pa_id,
                pa.order_line_id::text,
                pa.order_no,
                pa.supplier_name,
                pa.challan_no,
                pa.invoice_no,
                pa.document_date::text,
                pa.received_qty,
                pa.purchase_price,
                pa.total_value,
                pa.batch_no,
                pa.billing_status,
                pa.notes,
                pa.acknowledged_at::text
            FROM purchase_acknowledgements pa
            WHERE LOWER(pa.order_no) = LOWER(%(ono)s)
            ORDER BY pa.acknowledged_at DESC
        """, {"ono": _ono})

        # ── 2. Fetch order_lines with product + stage info ────────────────
        _ol_rows = _q2("""
            SELECT
                ol.id::text             AS line_id,
                ol.eye_side,
                p.product_name,
                COALESCE(ol.quantity,1) AS quantity,
                COALESCE(ol.gst_percent,0) AS gst_percent,
                ol.lens_params,
                ol.lens_params->>'manufacturing_route'  AS route,
                ol.lens_params->>'supplier_stage'       AS supplier_stage,
                ol.lens_params->>'replenishment_status' AS replenishment_status,
                ol.lens_params->>'procurement_status'   AS procurement_status
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            JOIN products p ON p.id = ol.product_id
            WHERE LOWER(o.order_no) = LOWER(%(ono)s)
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
            ORDER BY ol.eye_side
        """, {"ono": _ono})

        if not _pa_rows and not _ol_rows:
            st.error(f"No records found for order **{_ono}** in either table.")
            return

        # ── Index for quick lookup ─────────────────────────────────────────
        _pa_by_lid  = {r.get("order_line_id"): r for r in _pa_rows}
        _ol_by_lid  = {r.get("line_id"): r for r in _ol_rows}

        # ── 3. Stage check — detect lens_params not updated ───────────────
        _fixable = []
        for _ol in _ol_rows:
            _lid_v = _ol.get("line_id")
            _has_pa = _lid_v in _pa_by_lid
            _stage_ok = (
                str(_ol.get("supplier_stage") or "").upper() in ("READY_FOR_BILLING","READY_TO_BILL")
                or str(_ol.get("replenishment_status") or "").upper() == "PROCURED"
                or str(_ol.get("procurement_status") or "").upper() == "PROCURED"
            )
            if _has_pa and not _stage_ok:
                _fixable.append(_ol)

        # ── BANNER: PA exists but stage missing → show fix prominently ────
        if _fixable:
            st.markdown(
                f"<div style='background:#431407;border:2px solid #ea580c;"
                f"border-radius:8px;padding:12px 16px;margin-bottom:10px'>"
                f"<div style='color:#fb923c;font-weight:800;font-size:0.9rem'>"
                f"⚠️ Procurement was saved to DB but lens stage was NOT updated</div>"
                f"<div style='color:#fcd34d;font-size:0.78rem;margin-top:4px'>"
                f"{len(_fixable)} line(s) have a <code>purchase_acknowledgements</code> record "
                f"but <code>lens_params</code> stage is not set → order is invisible in Procured tab.</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if st.button(
                f"🔧 Fix Now — Set Stage for {len(_fixable)} Line(s) → Move to Procured Tab",
                key="dbchk_fix", type="primary", use_container_width=True
            ):
                _fix_count = 0
                for _fl in _fixable:
                    try:
                        _lp = _fl.get("lens_params") or {}
                        if isinstance(_lp, str):
                            try: _lp = json.loads(_lp)
                            except: _lp = {}
                        _lp = dict(_lp)
                        _r = str(_fl.get("route") or "").upper()
                        if _r in ("VENDOR","EXTERNAL_LAB"):
                            _lp["supplier_stage"] = "READY_FOR_BILLING"
                        elif _r == "STOCK":
                            _lp["replenishment_status"] = "PROCURED"
                        else:
                            _lp["procurement_status"] = "PROCURED"
                        _w2(
                            "UPDATE order_lines SET lens_params=%(lp)s::jsonb WHERE id=%(lid)s::uuid",
                            {"lp": json.dumps(_lp), "lid": _fl["line_id"]},
                        )
                        _fix_count += 1
                    except Exception as _fe:
                        st.error(f"Fix failed for {_fl.get('line_id','')[:8]}: {_fe}")
                if _fix_count:
                    st.success(
                        f"✅ Stage fixed for {_fix_count} line(s). "
                        f"Order **{_ono}** should now appear in the Procured tab."
                    )
                    st.rerun()

        # ── 4. Receipt view of stored procurement ─────────────────────────
        if _pa_rows:
            _ref_pa = _pa_rows[0]
            _doc_ref = _ref_pa.get("challan_no") or _ref_pa.get("invoice_no") or "—"
            _doc_date_disp = _ref_pa.get("document_date") or "—"
            _sup_disp = _hesc.escape(str(_ref_pa.get("supplier_name") or "—"))
            _saved_at = (_ref_pa.get("acknowledged_at") or "")[:16]

            st.markdown(
                f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
                f"border-radius:8px;padding:12px 16px;margin:8px 0'>"
                f"<div style='color:#10b981;font-size:0.88rem;font-weight:800;margin-bottom:6px'>"
                f"📋 Stored Purchase Receipt — {_ono}</div>"
                f"<div style='display:flex;gap:24px;flex-wrap:wrap;font-size:0.75rem;color:#94a3b8'>"
                f"<span><b style='color:#a5b4fc'>Supplier</b><br>"
                f"<span style='color:#e2e8f0;font-weight:600'>{_sup_disp}</span></span>"
                f"<span><b style='color:#a5b4fc'>Ref No</b><br>"
                f"<span style='color:#e2e8f0;font-weight:600'>{_hesc.escape(str(_doc_ref))}</span></span>"
                f"<span><b style='color:#a5b4fc'>Doc Date</b><br>"
                f"<span style='color:#e2e8f0'>{_doc_date_disp}</span></span>"
                f"<span><b style='color:#a5b4fc'>Saved At</b><br>"
                f"<span style='color:#e2e8f0'>{_saved_at}</span></span>"
                f"</div></div>",
                unsafe_allow_html=True,
            )

            # Column headers
            _rh = st.columns([3, 0.7, 1.3, 1.3, 1.3, 1.3])
            for _col, _lbl in zip(_rh, ["Product / Order","Qty","Price ₹/pc","Taxable ₹","GST ₹","Total ₹"]):
                _col.markdown(
                    f"<div style='font-size:0.65rem;font-weight:700;color:#475569;"
                    f"text-transform:uppercase;letter-spacing:.05em;"
                    f"border-bottom:1px solid #1e3a5f;padding-bottom:3px'>{_lbl}</div>",
                    unsafe_allow_html=True,
                )

            _receipt_lines = []
            _g_tax = _g_gst = _g_tot = 0.0

            for _idx, _pa in enumerate(_pa_rows):
                _lid_p    = _pa.get("order_line_id","")
                _ol_match = _ol_by_lid.get(_lid_p, {})
                _pname    = _hesc.escape(str(_ol_match.get("product_name") or "—"))
                _eye      = str(_ol_match.get("eye_side") or "ITEM").upper()
                _qty      = int(_pa.get("received_qty") or 1)
                _gst_pct  = float(_ol_match.get("gst_percent") or 0)
                _pa_id    = _pa.get("pa_id","")
                _bs       = str(_pa.get("billing_status") or "")
                _bs_color = "#10b981" if _bs.upper() in ("PURCHASE_ACKED","PROCURED","READY_FOR_BILLING","LOCKED") else "#ef4444"

                _rc1, _rc2, _rc3, _rc4, _rc5, _rc6 = st.columns([3, 0.7, 1.3, 1.3, 1.3, 1.3])
                with _rc1:
                    _batch_disp = _pa.get("batch_no") or ""
                    st.markdown(
                        f"<div style='font-size:0.8rem;color:#e2e8f0;font-weight:600'>"
                        f"{_eye} — {_pname}</div>"
                        f"<div style='font-size:0.67rem;color:#64748b'>"
                        f"<span style='color:{_bs_color}'>{_bs or 'NULL'}</span>"
                        + (f" · Batch: {_hesc.escape(_batch_disp)}" if _batch_disp else "")
                        + f"</div>",
                        unsafe_allow_html=True,
                    )
                with _rc2:
                    st.markdown(f"<div style='font-size:0.82rem;color:#94a3b8;padding-top:6px'>{_qty}</div>", unsafe_allow_html=True)

                # Editable price for paise correction
                with _rc3:
                    _stored_price = _r2(_pa.get("purchase_price") or 0)
                    _new_price = _r2(st.number_input(
                        "Price", value=_stored_price, min_value=0.0, step=0.01,
                        key=f"dbchk_price_{_idx}_{_pa_id}",
                        label_visibility="collapsed", format="%.2f",
                        help="Edit ±₹0.01 to correct paise difference for GST filing"
                    ))

                _taxable = _r2(_new_price * _qty)
                _gst_amt = _r2(_taxable * _gst_pct / 100)
                _ltot    = _r2(_taxable + _gst_amt)
                _g_tax   = _r2(_g_tax + _taxable)
                _g_gst   = _r2(_g_gst + _gst_amt)
                _g_tot   = _r2(_g_tot + _ltot)

                with _rc4:
                    st.markdown(f"<div style='font-size:0.82rem;color:#94a3b8;padding-top:6px'>₹{_taxable:,.2f}</div>", unsafe_allow_html=True)
                with _rc5:
                    _gst_lbl = f"{_gst_pct:.0f}%" if _gst_pct else "—"
                    st.markdown(f"<div style='font-size:0.82rem;color:#f59e0b;padding-top:6px'>₹{_gst_amt:,.2f} <span style='font-size:0.63rem;color:#475569'>({_gst_lbl})</span></div>", unsafe_allow_html=True)
                with _rc6:
                    _price_changed = abs(_new_price - _stored_price) > 0.001
                    _tot_color = "#fbbf24" if _price_changed else "#10b981"
                    st.markdown(f"<div style='font-size:0.85rem;color:{_tot_color};font-weight:700;padding-top:6px'>₹{_ltot:,.2f}</div>", unsafe_allow_html=True)

                _receipt_lines.append({
                    "pa_id":    _pa_id,
                    "lid":      _lid_p,
                    "qty":      _qty,
                    "price":    _new_price,
                    "total":    _r2(_new_price * _qty),
                    "stored_price": _stored_price,
                    "gst_pct":  _gst_pct,
                })

            # Totals row
            st.markdown("<div style='border-top:2px solid #1e3a5f;margin:4px 0 2px 0'></div>", unsafe_allow_html=True)
            _t1, _t2, _t3, _t4, _t5, _t6 = st.columns([3, 0.7, 1.3, 1.3, 1.3, 1.3])
            _t1.markdown(f"<div style='font-size:0.8rem;font-weight:700;color:#a5b4fc;padding-top:4px'>{len(_pa_rows)} line(s)</div>", unsafe_allow_html=True)
            _t4.markdown(f"<div style='font-size:0.8rem;font-weight:700;color:#94a3b8;padding-top:4px'>₹{_g_tax:,.2f}</div>", unsafe_allow_html=True)
            _t5.markdown(f"<div style='font-size:0.8rem;font-weight:700;color:#f59e0b;padding-top:4px'>₹{_g_gst:,.2f}</div>", unsafe_allow_html=True)
            _t6.markdown(f"<div style='font-size:0.93rem;font-weight:800;color:#10b981;padding-top:4px'>₹{_g_tot:,.2f}</div>", unsafe_allow_html=True)

            # GST split
            if _g_gst > 0:
                _cgst = _r2(_g_gst / 2)
                _sgst = _r2(_g_gst - _cgst)
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:5px;"
                    f"padding:6px 14px;margin-top:4px;font-size:0.75rem;color:#94a3b8'>"
                    f"<b style='color:#a5b4fc'>GST Split</b>&nbsp;·&nbsp;"
                    f"Taxable: <b style='color:#e2e8f0'>₹{_g_tax:,.2f}</b>&nbsp;·&nbsp;"
                    f"CGST: <b style='color:#f59e0b'>₹{_cgst:,.2f}</b>&nbsp;·&nbsp;"
                    f"SGST: <b style='color:#f59e0b'>₹{_sgst:,.2f}</b>&nbsp;·&nbsp;"
                    f"Grand Total incl. GST: <b style='color:#10b981'>₹{_g_tot:,.2f}</b>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

            # ── Price Amendment — only if any price was changed ───────────
            _amended = [r for r in _receipt_lines if abs(r["price"] - r["stored_price"]) > 0.001]
            if _amended:
                st.markdown(
                    f"<div style='background:#1a2744;border:1px solid #fbbf24;"
                    f"border-radius:6px;padding:8px 14px;margin-top:8px;font-size:0.77rem'>"
                    f"<b style='color:#fbbf24'>⚠️ Price Amended</b>&nbsp;"
                    f"<span style='color:#94a3b8'>{len(_amended)} line(s) changed — "
                    f"click below to update the stored record for GST paise correction.</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if st.button(
                    f"💾 Save Price Correction ({len(_amended)} line(s))",
                    key="dbchk_amend", use_container_width=True
                ):
                    _amend_ok = 0
                    for _ar in _amended:
                        _new_tot = _r2(_ar["price"] * _ar["qty"])
                        _ok = _w2("""
                            UPDATE purchase_acknowledgements
                            SET purchase_price  = %(price)s,
                                total_value     = %(total)s,
                                acknowledged_at = NOW()
                            WHERE id = %(paid)s::uuid
                        """, {
                            "price": _ar["price"],
                            "total": _new_tot,
                            "paid":  _ar["pa_id"],
                        })
                        if _ok:
                            _amend_ok += 1
                    if _amend_ok:
                        st.success(f"✅ Price correction saved for {_amend_ok} line(s).")
                        st.rerun()
            else:
                st.caption("✅ No price changes — receipt matches stored record.")

        elif _ol_rows:
            st.warning(f"⚠️ **Order found but procurement NOT saved.** No rows in `purchase_acknowledgements` for {_ono}.")
            st.caption("Go to the Procurement Queue, find this order, tick the line(s), and save.")


def _render_invoice_preview(sel_lines: list, doc_no: str, doc_type: str,
                             doc_date, sup_names: dict) -> list | None:
    """
    Editable invoice preview before final save.
    - price step = 0.01 for paise-level correction
    - all totals use ROUND_HALF_UP (avoids banker's rounding GST mismatches)
    - shows CGST/SGST split
    Returns the edited sel_lines on confirm, None if not yet confirmed.
    """
    import decimal as _dec

    def _r2(v):
        try:
            return float(_dec.Decimal(str(v)).quantize(_dec.Decimal("0.01"), rounding=_dec.ROUND_HALF_UP))
        except Exception:
            return round(float(v), 2)

    # Wrap preview in scrollable container so all lines + confirm button are visible
    st.markdown(
        "<div style='background:#0f172a;border:1px solid #6366f1;"
        "border-left:4px solid #10b981;border-radius:8px;"
        "padding:10px 16px;margin:10px 0 8px 0'>"
        "<span style='color:#10b981;font-size:0.92rem;font-weight:800'>"
        "📋 Purchase Invoice Preview — Review & Confirm</span>"
        "<span style='color:#475569;font-size:0.75rem;margin-left:10px'>"
        "Edit any price (step ₹0.01) to fix paise differences. "
        "Scroll down to see all lines and the Confirm button.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    _sup_name_disp = sup_names.get(sel_lines[0].get("supplier_id",""),"") if sel_lines else ""
    _h1, _h2, _h3 = st.columns([3, 2, 2])
    _h1.markdown(
        f"<div style='font-size:0.75rem;color:#94a3b8'>Supplier</div>"
        f"<div style='font-size:0.88rem;font-weight:700;color:#e2e8f0'>"
        f"{_sup_name_disp or '— No Supplier —'}</div>",
        unsafe_allow_html=True,
    )
    _h2.markdown(
        f"<div style='font-size:0.75rem;color:#94a3b8'>Ref No ({doc_type})</div>"
        f"<div style='font-size:0.88rem;font-weight:700;color:#e2e8f0'>{doc_no or '—'}</div>",
        unsafe_allow_html=True,
    )
    _h3.markdown(
        f"<div style='font-size:0.75rem;color:#94a3b8'>Date</div>"
        f"<div style='font-size:0.88rem;font-weight:700;color:#e2e8f0'>{doc_date}</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

    # Column headers
    _th = st.columns([3, 0.8, 1.4, 1.4, 1.4, 1.4])
    for _col, _lbl in zip(_th, ["Product / Order", "Qty", "Price ₹/pc", "Taxable ₹", "GST ₹", "Total ₹"]):
        _col.markdown(
            f"<div style='font-size:0.67rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.05em;"
            f"border-bottom:1px solid #1e3a5f;padding-bottom:3px'>{_lbl}</div>",
            unsafe_allow_html=True,
        )

    _edited_lines = []
    _grand_taxable = _grand_gst = _grand_total = 0.0

    for _idx, _item in enumerate(sel_lines):
        _ln         = _item["line"]
        _pname      = str(_ln.get("product_name","")).split(" | ")[0]
        _ono        = _ln.get("order_no","")
        _eye        = str(_ln.get("eye_side","")).upper() or "ITEM"
        _qty_orig   = int(_item.get("qty") or 1)
        _price_orig = float(_item.get("price") or 0)
        _gst_pct    = float(_ln.get("gst_percent") or 0)
        _lid_key    = _ln.get("line_id","")

        _pc1, _pc2, _pc3, _pc4, _pc5, _pc6 = st.columns([3, 0.8, 1.4, 1.4, 1.4, 1.4])
        with _pc1:
            st.markdown(
                f"<div style='font-size:0.8rem;color:#e2e8f0;font-weight:600'>"
                f"{_eye} — {_pname}</div>"
                f"<div style='font-size:0.67rem;color:#64748b'>{_ono}"
                + (f" · {_item.get('batch_no')}" if _item.get("batch_no") else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        with _pc2:
            _qty_edit = int(st.number_input(
                "Qty", value=float(_qty_orig), min_value=0.0, step=1.0,
                key=f"prev_qty_{_idx}_{_lid_key}",
                label_visibility="collapsed", format="%.0f",
            ))
        with _pc3:
            _price_edit = _r2(st.number_input(
                "Price", value=_price_orig, min_value=0.0, step=0.01,
                key=f"prev_price_{_idx}_{_lid_key}",
                label_visibility="collapsed", format="%.2f",
            ))

        _taxable  = _r2(_price_edit * _qty_edit)
        _gst_amt  = _r2(_taxable * _gst_pct / 100)
        _line_tot = _r2(_taxable + _gst_amt)
        _grand_taxable = _r2(_grand_taxable + _taxable)
        _grand_gst     = _r2(_grand_gst + _gst_amt)
        _grand_total   = _r2(_grand_total + _line_tot)

        with _pc4:
            st.markdown(f"<div style='font-size:0.82rem;color:#94a3b8;padding-top:6px'>₹{_taxable:,.2f}</div>", unsafe_allow_html=True)
        with _pc5:
            _gst_label = f"{_gst_pct:.0f}%" if _gst_pct else "—"
            st.markdown(f"<div style='font-size:0.82rem;color:#f59e0b;padding-top:6px'>₹{_gst_amt:,.2f} <span style='font-size:0.65rem;color:#475569'>({_gst_label})</span></div>", unsafe_allow_html=True)
        with _pc6:
            st.markdown(f"<div style='font-size:0.85rem;color:#10b981;font-weight:700;padding-top:6px'>₹{_line_tot:,.2f}</div>", unsafe_allow_html=True)

        _edited_item = dict(_item)
        _edited_item["qty"]   = _qty_edit
        _edited_item["price"] = _price_edit
        _edited_lines.append(_edited_item)

    # Totals row
    st.markdown("<div style='border-top:2px solid #1e3a5f;margin:6px 0 4px 0'></div>", unsafe_allow_html=True)
    _t1, _t2, _t3, _t4, _t5, _t6 = st.columns([3, 0.8, 1.4, 1.4, 1.4, 1.4])
    _t1.markdown(f"<div style='font-size:0.8rem;font-weight:700;color:#a5b4fc;padding-top:4px'>{len(sel_lines)} line(s)</div>", unsafe_allow_html=True)
    _t4.markdown(f"<div style='font-size:0.82rem;font-weight:700;color:#94a3b8;padding-top:4px'>₹{_grand_taxable:,.2f}</div>", unsafe_allow_html=True)
    _t5.markdown(f"<div style='font-size:0.82rem;font-weight:700;color:#f59e0b;padding-top:4px'>₹{_grand_gst:,.2f}</div>", unsafe_allow_html=True)
    _t6.markdown(f"<div style='font-size:0.95rem;font-weight:800;color:#10b981;padding-top:4px'>₹{_grand_total:,.2f}</div>", unsafe_allow_html=True)

    # GST split (CGST + SGST)
    if _grand_gst > 0:
        _cgst = _r2(_grand_gst / 2)
        _sgst = _r2(_grand_gst - _cgst)
        st.markdown(
            f"<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:5px;"
            f"padding:6px 14px;margin-top:6px;font-size:0.75rem;color:#94a3b8'>"
            f"<b style='color:#a5b4fc'>GST Split</b>&nbsp;·&nbsp;"
            f"Taxable: <b style='color:#e2e8f0'>₹{_grand_taxable:,.2f}</b>&nbsp;·&nbsp;"
            f"CGST: <b style='color:#f59e0b'>₹{_cgst:,.2f}</b>&nbsp;·&nbsp;"
            f"SGST: <b style='color:#f59e0b'>₹{_sgst:,.2f}</b>&nbsp;·&nbsp;"
            f"Total GST: <b style='color:#f59e0b'>₹{_grand_gst:,.2f}</b>&nbsp;·&nbsp;"
            f"Grand Total incl. GST: <b style='color:#10b981'>₹{_grand_total:,.2f}</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _cf1, _cf2, _cf3 = st.columns([4, 1, 1])
    _confirm = _cf1.button(
        f"✅ Confirm & Save Final  ({len(_edited_lines)} lines · ₹{_grand_total:,.2f})",
        type="primary", use_container_width=True, key="prx_confirm_save",
    )
    _cancel = _cf3.button("✖ Cancel", key="prx_cancel_preview", use_container_width=True)
    if _cancel:
        st.session_state["prx_preview_mode"] = False
        st.rerun()
    if _confirm:
        return _edited_lines
    return None


def _load_procurement_analytics(days: int = 90) -> list:
    """Supplier performance and purchase analytics. Cached to keep UI snappy."""
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT
                COALESCE(pa.supplier_name, pt.party_name, 'Unknown') AS supplier_name,
                COUNT(*) AS lines,
                SUM(COALESCE(pa.received_qty, pa.qty, 0)) AS qty,
                SUM(COALESCE(pa.total_value, COALESCE(pa.purchase_price,0) * COALESCE(pa.received_qty, pa.qty, 0))) AS purchase_value,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(pa.billing_status,'')) IN ('PROCURED','PURCHASE_ACKED','READY','LOCKED')) AS procured_lines,
                COUNT(*) FILTER (WHERE pa.batch_no IS NOT NULL OR pa.notes IS NOT NULL) AS docs_attached,
                MAX(pa.acknowledged_at)::date AS last_purchase_date
            FROM purchase_acknowledgements pa
            LEFT JOIN parties pt ON pt.id = pa.supplier_id
            WHERE COALESCE(pa.acknowledged_at, CURRENT_DATE)::date >= CURRENT_DATE - (%(days)s || ' days')::interval
            GROUP BY COALESCE(pa.supplier_name, pt.party_name, 'Unknown')
            ORDER BY purchase_value DESC NULLS LAST, lines DESC
            LIMIT 100
        """, {"days": int(days)}) or []
    except Exception as _e:
        return []



def _render_procurement_analytics() -> None:
    """Procurement analytics: supplier performance, volume, document compliance."""
    st.markdown("### 📦 Procurement Analytics")
    st.caption("Supplier-wise purchase value, line count, and invoice/challan document compliance.")
    a1, a2 = st.columns([2, 1])
    with a1:
        days = st.selectbox("Period", [30, 60, 90, 180, 365], index=2, key="pa_days")
    with a2:
        if st.button("Refresh Analytics", key="pa_refresh", use_container_width=True):
            try: _load_procurement_analytics.clear()
            except Exception: pass
            st.rerun()

    rows = _load_procurement_analytics(int(days))
    if not rows:
        st.info("No procurement data found for the selected period.")
        return

    total_value = sum(float(r.get("purchase_value") or 0) for r in rows)
    total_lines = sum(int(r.get("lines") or 0) for r in rows)
    doc_lines = sum(int(r.get("docs_attached") or 0) for r in rows)
    k1, k2, k3 = st.columns(3)
    k1.metric("Purchase Value", f"₹{total_value:,.2f}")
    k2.metric("Purchase Lines", total_lines)
    k3.metric("Docs Attached", f"{doc_lines}/{total_lines}")

    st.markdown("#### Supplier Performance")
    for r in rows[:30]:
        name = r.get("supplier_name") or "Unknown"
        lines = int(r.get("lines") or 0)
        qty = int(r.get("qty") or 0)
        val = float(r.get("purchase_value") or 0)
        docs = int(r.get("docs_attached") or 0)
        last = r.get("last_purchase_date") or "—"
        doc_pct = (docs / lines * 100) if lines else 0
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([4, 1.2, 1.2, 1.5, 1.5])
            c1.markdown(f"**{name}**  \nLast: {last}")
            c2.metric("Lines", lines)
            c3.metric("Qty", qty)
            c4.metric("Value", f"₹{val:,.0f}")
            c5.metric("Docs", f"{doc_pct:.0f}%")



# ==================================================
# SUPPLIER PIPELINE
# ==================================================



def _render_procurement_rx():
    """
    Procurement Queue — Central pending purchase manager screen.

    All stock/vendor/lab/ophthalmic lines that need purchase acknowledgement.
    Staff ticks lines, picks supplier, enters challan/invoice, uploads PDF, saves.
    Single screen for all routes. No navigation needed.
    """
    from collections import defaultdict

    def _q2(sql, params=None):
        try:
            from modules.sql_adapter import run_query
            return run_query(sql, params or {}) or []
        except Exception as e:
            st.error(f"DB: {e}"); return []

    def _w2(sql, params=None):
        try:
            from modules.sql_adapter import run_write
            run_write(sql, params or {}); return True
        except Exception as e:
            st.error(f"Write: {e}"); return False

    def _qty_box_pcs_label(qty, box_size, unit="PCS") -> str:
        try:
            q = float(qty or 0)
            b = float(box_size or 1)
        except Exception:
            return f"{qty or 0} PCS"
        unit_u = str(unit or "PCS").upper()
        if b > 1:
            boxes = q / b
            whole = int(q // b)
            rem = int(q % b)
            if rem:
                return f"{whole} BOX + {rem} PCS / {int(q)} PCS"
            return f"{boxes:g} BOX / {int(q)} PCS"
        return f"{q:g} {unit_u}"

    def _render_power_stock_hint(_ln):
        """Show existing stock/new-receipt hint for checked contact lens lines."""
        _pwr_d = {}
        for _pk in ("sph","cyl","axis","add_power"):
            _pv = _ln.get(_pk)
            if _pv and str(_pv) not in ("","0","0.0","None"):
                try:
                    _pwr_d[_pk.replace("add_power","add")] = float(_pv)
                except Exception:
                    pass
        if not _pwr_d:
            return
        _pid2   = str(_ln.get("product_id") or "")
        _mgrp2  = str(_ln.get("main_group") or "").lower()
        _is_cl2 = "contact" in _mgrp2 or "cl" in _mgrp2
        _eye2   = str(_ln.get("eye_side") or "")
        _pname2 = str(_ln.get("product_name") or "")
        if not (_is_cl2 and _pid2):
            return
        try:
            from modules.procurement.supplier_invoice_rules import match_to_inventory_stock
            _si2 = match_to_inventory_stock(_pid2, _pwr_d, True)
            if _si2:
                _av2 = int(_si2.get("qty") or 0)
                st.markdown(
                    f"<div style='background:#052e16;border:1px solid #166534;"
                    f"border-radius:4px;padding:3px 10px;font-size:0.72rem;"
                    f"color:#86efac;margin:2px 0'>"
                    f"📦 {_eye2} {_pname2}: Batch {_si2.get('batch_no','—')}"
                    f" · Avail {_av2} PCS · Exp {_si2.get('expiry_date','—')}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='background:#1a0a00;border:1px solid #78350f;"
                    f"border-radius:4px;padding:3px 10px;font-size:0.72rem;"
                    f"color:#fcd34d;margin:2px 0'>"
                    f"🆕 {_eye2} {_pname2}: New power variant — receipt adds to stock"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        except Exception as _stock_hint_err:
            st.caption(f"Stock hint unavailable: {_stock_hint_err}")

    # ── Fix: restore page scrollbar (can be hidden by Streamlit theme overrides) ──
    st.markdown(
        """<style>
        /* Ensure vertical scrollbar is always visible */
        html, body, [data-testid="stAppViewContainer"] {
            overflow-y: auto !important;
        }
        /* Prevent horizontal overflow from cramped columns */
        [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap;
            overflow-x: auto;
        }
        /* Scrollable procurement lines zone */
        .prx-lines-scroll {
            max-height: 62vh;
            overflow-y: auto;
            overflow-x: hidden;
            padding-right: 4px;
            scrollbar-width: thin;
            scrollbar-color: #334155 #0f172a;
        }
        .prx-lines-scroll::-webkit-scrollbar { width: 6px; }
        .prx-lines-scroll::-webkit-scrollbar-track { background: #0f172a; }
        .prx-lines-scroll::-webkit-scrollbar-thumb {
            background: #334155; border-radius: 4px;
        }
        /* Product name line — prevent truncation */
        .prx-product-name {
            white-space: normal !important;
            word-break: break-word;
            font-size: 0.88rem;
            color: #111827;
            font-weight: 900;
        }
        .prx-product-meta {
            font-size: 0.74rem;
            color: #374151;
            font-weight: 650;
            line-height: 1.5;
        }
        .prx-sup-info {
            font-size: 0.71rem;
            color: #38bdf8;
            font-weight: 600;
        }
        </style>""",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div style='background:#0f172a;border:1px solid #1e3a5f;"
        "border-left:4px solid #6366f1;border-radius:8px;"
        "padding:10px 16px;margin-bottom:12px'>"
        "<span style='color:#a5b4fc;font-size:1rem;font-weight:800'>📥 Procurement Queue</span>"
        "<span style='color:#475569;font-size:0.78rem;margin-left:10px'>"
        "Pending purchase lines from Stock, Supplier, External Lab and RX. "
        "Select supplier → send order → upload invoice/challan → save.</span></div>",
        unsafe_allow_html=True,
    )

    # ── DB Diagnostic (always visible — staff can check any order) ──────
    _render_order_db_check()

    # ── Filters ──────────────────────────────────────────────────────────
    with st.container(border=True):
        _f1, _f2, _f3, _f4 = st.columns([3, 2, 2, 2])
        _srch = _f1.text_input("Search", placeholder="🔍 Order / patient / product",
                                key="prx_search", label_visibility="collapsed")
        _route = _f2.selectbox("Route", ["ALL","STOCK","VENDOR","EXTERNAL_LAB"],
                                key="prx_route", label_visibility="collapsed")
        _dfrom = _f3.date_input("From",
                                 value=_dt.date.today() - _dt.timedelta(days=60),
                                 key="prx_dfrom", label_visibility="collapsed",
                                 format="DD/MM/YYYY")
        _dto   = _f4.date_input("To", value=_dt.date.today(),
                                 key="prx_dto", label_visibility="collapsed",
                                 format="DD/MM/YYYY")

    # ── Query ─────────────────────────────────────────────────────────────
    _where = [
        "COALESCE(ol.is_deleted, FALSE) = FALSE",
        "COALESCE(ol.is_service_line, FALSE) = FALSE",
        "UPPER(COALESCE(ol.eye_side,'')) NOT IN ('S','SERVICE','O')",
        # Procurement readiness is line-stage driven. Some legacy orders keep
        # order.status as PENDING even after supplier/stock line stages move
        # forward, so only hard-stop cancelled/held orders here.
        "o.status NOT IN ('CANCELLED','CLOSED','HOLD','CREDIT_HOLD')",
        """(
            (
                UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = 'STOCK'
                AND UPPER(COALESCE(ol.lens_params->>'replenishment_status','')) = 'ORDERED'
            )
            OR (
                UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) IN ('VENDOR','EXTERNAL_LAB')
                AND UPPER(COALESCE(ol.lens_params->>'supplier_stage','')) IN (
                    'SUPPLIER_CONFIRMED',
                    'AWAITING_SUPPLY',
                    'RECEIVED',
                    'INSPECTION',
                    'READY_FOR_BILLING',
                    'READY_TO_BILL'
                )
            )
        )""",
        "o.created_at::date BETWEEN %(df)s AND %(dt)s",
        # Procurement starts from assignment/reservation, not customer billing.
        # Supplier/External/RX lines and reserved stock should be orderable
        # before challan, so staff can receive purchase documents first.
        # Use GREATEST so that allocated_qty=0 never hides a line that has
        # billed_qty or quantity > 0 (the old COALESCE would stop at 0 and
        # never reach the next non-zero field).
        """GREATEST(
            COALESCE(ol.allocated_qty, 0),
            COALESCE(ol.billed_qty,    0),
            COALESCE(ol.quantity,      0)
        ) > 0""",
        """NOT EXISTS (
            SELECT 1 FROM purchase_acknowledgements pa
            WHERE pa.order_line_id = ol.id
              AND (
                COALESCE(pa.purchase_price, 0) > 0
                OR UPPER(COALESCE(pa.billing_status,'')) IN ('PURCHASE_ACKED','PROCURED','READY_FOR_BILLING','LOCKED')
              )
        )""",
    ]
    _qp = {"df": str(_dfrom), "dt": str(_dto)}

    if _route != "ALL":
        _where.append("UPPER(COALESCE(ol.lens_params->>'manufacturing_route','')) = %(route)s")
        _qp["route"] = _route

    if _srch.strip():
        _where.append(
            "(LOWER(o.order_no) LIKE %(s)s"
            " OR LOWER(COALESCE(o.patient_name,o.party_name,'')) LIKE %(s)s"
            " OR LOWER(p.product_name) LIKE %(s)s)"
        )
        _qp["s"] = f"%{_srch.strip().lower()}%"

    # Procurement queue query — cached 45s to prevent re-querying on every
    # widget interaction. Cache is busted after a successful save (see post-save).
    _prx_cache_key = f"_prx_rows_{_route}_{_srch}_{_dfrom}_{_dto}"
    if _prx_cache_key not in st.session_state:
        st.session_state[_prx_cache_key] = None  # will be set below

    _rows = _q2(f"""
        SELECT
            o.id::text AS order_id, o.order_no,
            COALESCE(o.patient_name, o.party_name, '—') AS patient_name,
            o.created_at,
            ol.id::text AS line_id, ol.eye_side,
            COALESCE(ol.quantity, 1) AS quantity,
            COALESCE(ol.allocated_qty, 0) AS allocated_qty,
            COALESCE(NULLIF(ol.billed_qty,0), NULLIF(ol.allocated_qty,0), ol.quantity, 1) AS billed_qty,
            COALESCE(p.box_size, 1) AS box_size,
            COALESCE(ol.unit_price, 0) AS unit_price,
            COALESCE(ol.discount_amount, 0) AS discount_amount,
            COALESCE(ol.gst_percent, 0)    AS gst_percent,
            ol.lens_params, ol.sph, ol.cyl, ol.axis, ol.add_power,
            p.id::text AS product_id,
            COALESCE(p.product_name,'') AS product_name,
            COALESCE(p.brand,'') AS brand,
            COALESCE(p.category,'') AS category,
            COALESCE(ol.lens_params->>'manufacturing_route','') AS route,
            COALESCE(ol.lens_params->>'supplier_name','') AS mapped_supplier_name,
            COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '') AS mapped_supplier_id,
            COALESCE((
                SELECT psm.supplier_product_name
                FROM product_supplier_map psm
                WHERE psm.product_id = ol.product_id
                  AND psm.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm.is_active, TRUE)=TRUE
                ORDER BY psm.rank ASC
                LIMIT 1
            ), '') AS supplier_product_name,
            COALESCE((
                SELECT psm.supplier_brand
                FROM product_supplier_map psm
                WHERE psm.product_id = ol.product_id
                  AND psm.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm.is_active, TRUE)=TRUE
                ORDER BY psm.rank ASC
                LIMIT 1
            ), '') AS supplier_brand,
            COALESCE((
                SELECT psm.supplier_index
                FROM product_supplier_map psm
                WHERE psm.product_id = ol.product_id
                  AND psm.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm.is_active, TRUE)=TRUE
                ORDER BY psm.rank ASC
                LIMIT 1
            ), '') AS supplier_index,
            COALESCE((
                SELECT psm.supplier_coating
                FROM product_supplier_map psm
                WHERE psm.product_id = ol.product_id
                  AND psm.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm.is_active, TRUE)=TRUE
                ORDER BY psm.rank ASC
                LIMIT 1
            ), '') AS supplier_coating,
            COALESCE((
                SELECT psm.supplier_treatment
                FROM product_supplier_map psm
                WHERE psm.product_id = ol.product_id
                  AND psm.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm.is_active, TRUE)=TRUE
                ORDER BY psm.rank ASC
                LIMIT 1
            ), '') AS supplier_treatment,
            COALESCE((
                SELECT psm.supplier_id::text FROM product_supplier_map psm
                WHERE psm.product_id=ol.product_id
                ORDER BY psm.supplier_id LIMIT 1
            ), ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '') AS preferred_supplier_id,
            COALESCE((
                SELECT COALESCE(NULLIF(s.purchase_price,0), s.purchase_rate, 0)
                FROM inventory_stock s WHERE s.product_id=ol.product_id
                  AND COALESCE(s.is_active,TRUE)=TRUE
                  AND COALESCE(NULLIF(s.purchase_price,0), s.purchase_rate, 0) > 0
                ORDER BY s.created_at DESC LIMIT 1
            ), (
                SELECT ROUND(COALESCE(NULLIF(osm.purchase_rate,0), 0)::numeric / 2.0, 2)
                FROM product_supplier_map psm_price
                JOIN products sp
                  ON LOWER(COALESCE(sp.product_name,'')) = LOWER(COALESCE(psm_price.supplier_product_name,''))
                 AND (
                        COALESCE(psm_price.supplier_brand,'') = ''
                     OR LOWER(COALESCE(sp.brand,'')) = LOWER(COALESCE(psm_price.supplier_brand,''))
                 )
                JOIN ophthalmic_lens_specs osm
                  ON osm.product_id = sp.id
                WHERE psm_price.product_id = ol.product_id
                  AND psm_price.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm_price.is_active, TRUE)=TRUE
                  AND (
                        COALESCE(psm_price.supplier_index,'') = ''
                     OR osm.index_value::text = psm_price.supplier_index
                  )
                  AND (
                        COALESCE(psm_price.supplier_coating,'') = ''
                     OR osm.coating = psm_price.supplier_coating
                  )
                  AND COALESCE(osm.treatment, 'Clear') = COALESCE(NULLIF(psm_price.supplier_treatment,''), 'Clear')
                  AND COALESCE(osm.is_active, TRUE)=TRUE
                  AND COALESCE(osm.purchase_rate,0) > 0
                ORDER BY osm.updated_at DESC NULLS LAST, osm.created_at DESC NULLS LAST
                LIMIT 1
            ), (
                SELECT COALESCE(NULLIF(sm.purchase_price,0), sm.purchase_rate, 0)
                FROM product_supplier_map psm_stock
                JOIN products sp2
                  ON LOWER(COALESCE(sp2.product_name,'')) = LOWER(COALESCE(psm_stock.supplier_product_name,''))
                 AND (
                        COALESCE(psm_stock.supplier_brand,'') = ''
                     OR LOWER(COALESCE(sp2.brand,'')) = LOWER(COALESCE(psm_stock.supplier_brand,''))
                 )
                JOIN inventory_stock sm
                  ON sm.product_id = sp2.id
                WHERE psm_stock.product_id = ol.product_id
                  AND psm_stock.supplier_id::text = COALESCE(ol.lens_params->>'supplier_id', ol.lens_params->>'replenishment_supplier_id', '')
                  AND COALESCE(psm_stock.is_active, TRUE)=TRUE
                  AND COALESCE(sm.is_active, TRUE)=TRUE
                  AND COALESCE(NULLIF(sm.purchase_price,0), sm.purchase_rate, 0) > 0
                ORDER BY sm.created_at DESC LIMIT 1
            ), (
                SELECT ROUND(COALESCE(NULLIF(os.purchase_rate,0), 0)::numeric / 2.0, 2)
                FROM ophthalmic_lens_specs os
                WHERE os.product_id = ol.product_id
                  AND os.index_value::text = COALESCE(
                        ol.lens_params->>'lens_index',
                        ol.lens_params->>'index',
                        p.index_value::text,
                        ''
                      )
                  AND os.coating = COALESCE(
                        ol.lens_params->>'coating',
                        ol.lens_params->>'coating_type',
                        p.coating_type,
                        ''
                      )
                  AND COALESCE(os.treatment, 'Clear') = COALESCE(
                        NULLIF(ol.lens_params->>'treatment',''),
                        'Clear'
                      )
                  AND COALESCE(os.is_active, TRUE)=TRUE
                  AND COALESCE(os.purchase_rate,0) > 0
                LIMIT 1
            ), (
                SELECT COALESCE(NULLIF(s2.purchase_price,0), s2.purchase_rate, 0)
                FROM inventory_stock s2
                WHERE s2.product_id=ol.product_id
                  AND s2.stock_type='PRICE'
                  AND COALESCE(s2.is_price_current, TRUE)=TRUE
                  AND COALESCE(s2.is_active, TRUE)=TRUE
                  AND COALESCE(NULLIF(s2.purchase_price,0), s2.purchase_rate, 0) > 0
                ORDER BY s2.effective_from DESC NULLS LAST, s2.updated_at DESC NULLS LAST
                LIMIT 1
            ), 0)::numeric AS last_price
        FROM order_lines ol
        JOIN orders o ON o.id=ol.order_id
        JOIN products p ON p.id=ol.product_id
        WHERE {' AND '.join(_where)}
        ORDER BY o.created_at DESC, o.order_no,
                 CASE WHEN ol.eye_side='R' THEN 0 WHEN ol.eye_side='L' THEN 1 ELSE 2 END
        LIMIT 500
    """, _qp)

    if not _rows:
        st.success("✅ No lines pending procurement in this period.")
        return

    # NOTE: ensure_queue_items() removed from render path.
    # It was calling _line_snapshot() once per row (up to 500 DB queries per render).
    # It now only runs after a successful save. See post-save block below.

    # ── Suppliers list ────────────────────────────────────────────────────
    _sups = _q2(
        "SELECT id::text AS id, party_name, COALESCE(mobile,'') AS mobile, COALESCE(email,'') AS email FROM parties "
        "WHERE UPPER(COALESCE(party_type,'')) IN "
        "('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB','CONTACT_LENS_SUPPLIER') "
        "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name"
    )
    _sup_ids   = [""] + [s["id"] for s in _sups]
    _sup_names = {"": "— Select Supplier —"}
    _sup_names.update({s["id"]: s["party_name"] for s in _sups})
    _sup_by_id = {"": {"party_name": "— Select Supplier —", "mobile": "", "email": ""}}
    _sup_by_id.update({s["id"]: s for s in _sups})

    st.caption(f"**{len(_rows)} line(s)** pending procurement · "
               f"{len(set(r['order_no'] for r in _rows))} order(s)")

    _sel_lines: list = []

    # ── Scrollable line list ───────────────────────────────────────────────
    st.markdown("<div class='prx-lines-scroll'>", unsafe_allow_html=True)

    # ── Batch entry panel ─────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown(
            "<div style='color:#a5b4fc;font-size:0.8rem;font-weight:700;"
            "margin-bottom:8px'>⚡ Batch Purchase Entry</div>",
            unsafe_allow_html=True,
        )

        # ── Power-aware stock check ────────────────────────────────────────
        # For CL lines: show existing stock row for this product+power.
        # Warns if new power variant being received (will add to inventory).
        st.caption("Tick lines below to see power-aware stock hints and enter batch/expiry.")

        _bc1, _bc2, _bc3, _bc4 = st.columns([3, 2, 2, 2])
        _batch_note = _bc1.text_input(
            "📝 Purchase Note (optional)",
            key="prx_batch_note",
            placeholder="Internal note for this batch",
            label_visibility="collapsed",
        )
        _doc_type   = _bc2.selectbox("Type", ["CHALLAN","INVOICE","BOTH"],
                                      key="prx_doc_type")
        _doc_no     = _bc3.text_input("Challan / Invoice No",
                                       key="prx_doc_no", placeholder="Ref number")
        _doc_date   = _bc4.date_input("Date", value=_dt.date.today(),
                                       key="prx_doc_date", format="DD/MM/YYYY")
        # Batch/expiry entered per-line (below each selected line)

        _uploaded = st.file_uploader(
            "📎 Attach supplier invoice / challan (PDF or image — stored for audit)",
            type=["pdf","png","jpg","jpeg","webp"],
            key="prx_upload", label_visibility="collapsed",
        )
        _inv_b64, _inv_name, _inv_disk_path = "", "", ""
        if _uploaded:
            _inv_name = _uploaded.name
            _inv_b64  = base64.b64encode(_uploaded.getvalue()).decode()
            # Save to disk — primary storage; b64 is fallback/audit copy
            try:
                import pathlib as _plib, datetime as _invdt
                _inv_dir = _plib.Path("uploads/purchase_invoices")
                _inv_dir.mkdir(parents=True, exist_ok=True)
                _safe_fn = (
                    _invdt.date.today().isoformat() + "_"
                    + (_doc_no.strip() or "ref").replace("/","-").replace(" ","_")
                    + "_" + _inv_name
                )
                _inv_disk_path = str(_inv_dir / _safe_fn)
                with open(_inv_disk_path, "wb") as _ifh:
                    _ifh.write(_uploaded.getvalue())
            except Exception as _e:
                _inv_disk_path = ""
            if _uploaded.type.startswith("image"):
                st.image(_uploaded, caption=_inv_name, width=300)
            else:
                st.success(f"📄 {_inv_name} ({len(_uploaded.getvalue())//1024} KB) attached")
            if _inv_disk_path:
                with st.expander("🔍 Scan / OCR Review", expanded=True):
                    try:
                        from modules.procurement.invoice_review_panel import render_invoice_scan_review_panel
                        render_invoice_scan_review_panel(_inv_disk_path, supplier_id="", supplier_name="", key_prefix="prx_scan_review")
                    except Exception as _scan_err:
                        st.warning(f"Scan review unavailable: {_scan_err}")

    # ── Line selection ────────────────────────────────────────────────────
    _grouped = defaultdict(list)
    for _r in _rows:
        _grouped[_r["order_no"]].append(_r)

    for _ono, _lines in _grouped.items():
        _pat = _lines[0].get("patient_name","—")
        st.markdown(
            f"<div style='border-top:2px solid #1e3a5f;margin:10px 0 4px 0;"
            f"display:flex;align-items:center;gap:8px'>"
            f"<span style='background:#0a1628;color:#475569;font-size:0.65rem;"
            f"font-weight:700;padding:0 8px;border:1px solid #1e3a5f;"
            f"border-radius:3px;white-space:nowrap'>"
            f"📋 {_ono} — {_pat}</span></div>",
            unsafe_allow_html=True,
        )

        for _ln in _lines:
            _lid    = _ln["line_id"]
            _eye    = str(_ln.get("eye_side","")).upper()
            _pname  = str(_ln.get("product_name","")).split(" | ")[0]
            _sup_pname = str(_ln.get("supplier_product_name") or "").strip()
            _sup_brand = str(_ln.get("supplier_brand") or "").strip()
            _route_l = str(_ln.get("route","")).upper()
            _qty    = int(_ln.get("allocated_qty") or _ln.get("billed_qty") or _ln.get("quantity") or 1)
            _box_size = int(float(_ln.get("box_size") or 1))
            _qty_label = _qty_box_pcs_label(_qty, _box_size, _ln.get("unit") or "PCS")
            _disc   = float(_ln.get("discount_amount") or 0)
            _gross  = float(_ln.get("unit_price") or 0) * _qty
            _net    = round(_gross - _disc, 2)
            _last_p = float(_ln.get("last_price") or 0)

            # Power
            _pwr_bits = []
            for _pk, _pl in [("sph","S"),("cyl","C"),("axis","AX"),("add_power","ADD")]:
                _pv = _ln.get(_pk)
                if _pv and str(_pv) not in ("","0","0.0","None"):
                    try:
                        _pf = float(_pv)
                        if _pk == "cyl" and abs(_pf) < 0.01: continue
                        _pwr_bits.append(f"{_pl}{_pf:+.2f}" if _pk not in ("axis",) else f"AX{int(_pf)}")
                    except: pass
            _pwr_str = " ".join(_pwr_bits)
            _sup_desc_bits = []
            if _sup_brand:
                _sup_desc_bits.append(_sup_brand)
            if _sup_pname:
                _sup_desc_bits.append(_sup_pname)
            if _ln.get("supplier_index"):
                _sup_desc_bits.append(f"Index {_ln.get('supplier_index')}")
            if _ln.get("supplier_coating"):
                _sup_desc_bits.append(str(_ln.get("supplier_coating")))
            if _ln.get("supplier_treatment") and str(_ln.get("supplier_treatment")) != "Clear":
                _sup_desc_bits.append(str(_ln.get("supplier_treatment")))
            _sup_desc = "Supplier Product: " + " · ".join(_sup_desc_bits) if _sup_desc_bits else ""

            # Preferred supplier
            _pref_sid = str(_ln.get("preferred_supplier_id") or
                            _ln.get("mapped_supplier_id") or "")
            _pref_idx = _sup_ids.index(_pref_sid) if _pref_sid in _sup_ids else 0

            _lc1, _lc2, _lc3, _lc4 = st.columns([0.5, 4.5, 3, 2])

            with _lc1:
                _chk = st.checkbox("", key=f"prx_chk_{_lid}",
                                    label_visibility="collapsed")

            with _lc2:
                _rtclr = {"STOCK":"#0d9488","VENDOR":"#3b82f6","EXTERNAL_LAB":"#a855f7"}.get(_route_l,"#64748b")
                _eye_label = _eye if _eye else "ITEM"
                # Safely escape any special HTML chars in product name
                import html as _html_mod
                _pname_safe = _html_mod.escape(_pname)
                _sup_desc_safe = _html_mod.escape(_sup_desc) if _sup_desc else ""
                _brand_safe  = _html_mod.escape(str(_ln.get("brand","")) or "")
                _pwr_safe    = _html_mod.escape(_pwr_str) if _pwr_str else ""

                _meta_parts = []
                if _pwr_safe:
                    _meta_parts.append(_pwr_safe)
                if _brand_safe:
                    _meta_parts.append(_brand_safe)
                _meta_parts.append(f"Qty {_qty_label}")
                if _disc > 0:
                    _meta_parts.append(f"Disc ₹{_disc:.2f}")
                _meta_str = " · ".join(_meta_parts)

                _sup_html = (
                    f"<div class='prx-sup-info'>{_sup_desc_safe}</div>"
                    if _sup_desc_safe else ""
                )
                st.markdown(
                    f"<div>"
                    f"<span class='prx-product-name'>{_eye_label} — {_pname_safe}</span>"
                    f"&nbsp;<span style='background:{_rtclr}22;color:{_rtclr};"
                    f"font-size:0.63rem;padding:1px 6px;border-radius:3px;"
                    f"vertical-align:middle;white-space:nowrap'>{_route_l}</span>"
                    f"<div class='prx-product-meta'>{_meta_str}</div>"
                    f"{_sup_html}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

            with _lc3:
                _sel_sup = st.selectbox(
                    "Supplier",
                    _sup_ids,
                    index=_pref_idx,
                    format_func=lambda x: _sup_names.get(x,x),
                    key=f"prx_sup_{_lid}",
                    label_visibility="collapsed",
                )

            with _lc4:
                _price = st.number_input(
                    "Purchase ₹/pc",
                    value=_last_p or 0.0,
                    min_value=0.0, step=0.5, format="%.2f",
                    key=f"prx_price_{_lid}",
                    label_visibility="collapsed",
                )

            # Per-line batch/expiry/received qty (show when checked)
            _line_batch  = ""
            _line_expiry = None
            _line_recv   = _qty
            _line_sup_item = _sup_pname
            if _chk:
                _render_power_stock_hint(_ln)
                with st.container():
                    _pl1, _pl2, _pl3, _pl4 = st.columns([2, 2, 2, 3])
                    _line_batch  = _pl1.text_input(
                        "📦 Batch No.",
                        key=f"prx_batch_{_lid}",
                        placeholder="Required for CL",
                        label_visibility="collapsed",
                    )
                    _line_expiry = _pl2.date_input(
                        "📅 Expiry",
                        value=None,
                        key=f"prx_expiry_{_lid}",
                        format="DD/MM/YYYY",
                        label_visibility="collapsed",
                    )
                    # For box products (box_size > 1), default and unit is BOXES.
                    # Price is per box. Total = boxes × price_per_box.
                    _recv_default = float(_qty // _box_size) if _box_size > 1 else float(_qty)
                    _recv_unit    = "boxes" if _box_size > 1 else "pcs"
                    _line_recv = _pl3.number_input(
                        f"Qty Received ({_recv_unit})",
                        value=_recv_default, min_value=0.0, step=1.0,
                        key=f"prx_recv_{_lid}",
                        label_visibility="collapsed",
                    )
                    _line_sup_item = _pl4.text_input(
                        "Supplier item on bill",
                        value=_sup_pname,
                        key=f"prx_supplier_item_{_lid}",
                        placeholder="Supplier product name as printed on invoice",
                        label_visibility="collapsed",
                    )
                    _recv_pcs_total = int(_line_recv * _box_size) if _box_size > 1 else int(_line_recv)
                    _recv_caption   = (
                        f"{int(_line_recv)} box(es) × {_box_size} pcs = {_recv_pcs_total} pcs total"
                        if _box_size > 1 else f"{int(_line_recv)} pcs"
                    )
                    st.caption(
                        f"Receiving: {_recv_caption} · "
                        f"Total: ₹{float(_price) * float(_line_recv):,.2f} · "
                        f"Batch/expiry saved with this purchase line."
                    )

            if _chk:
                # qty stored in BOXES for box products — price is per box.
                # pcs_qty is derived for reference only (not used for total).
                _qty_to_save  = int(_line_recv)   # already in boxes if box_size > 1
                _pcs_for_ref  = _qty_to_save * _box_size if _box_size > 1 else _qty_to_save
                _sel_lines.append({
                    "line":         _ln,
                    "supplier_id":  _sel_sup,
                    "supplier_name":_sup_names.get(_sel_sup,""),
                    "price":        _price,
                    "qty":          _qty_to_save,   # boxes (or pcs if box_size=1)
                    "qty_pcs":      _pcs_for_ref,   # informational
                    "box_size":     _box_size,
                    "batch_no":     _line_batch.strip(),
                    "expiry_date":  str(_line_expiry or ""),
                    "supplier_item": _line_sup_item.strip(),
                    "gst_percent":  float(_ln.get("gst_percent") or 0),
                    "category":     str(_ln.get("category") or "LENSES").upper(),
                })

    # ── Close scrollable zone ─────────────────────────────────────────────
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Summary + Preview → Confirm → Save ──────────────────────────────
    if _sel_lines:
        import decimal as _dec_top
        def _r2_top(v):
            try:
                return float(_dec_top.Decimal(str(v)).quantize(
                    _dec_top.Decimal("0.01"), rounding=_dec_top.ROUND_HALF_UP))
            except Exception:
                return round(float(v), 2)

        _total_val = sum(_r2_top(item["price"] * item["qty"]) for item in _sel_lines)
        _selected_suppliers = sorted({
            str(item.get("supplier_name") or "").strip()
            for item in _sel_lines
            if str(item.get("supplier_id") or "").strip()
        })
        _missing_supplier = [
            item for item in _sel_lines
            if not str(item.get("supplier_id") or "").strip()
        ]
        st.markdown("---")
        _supplier_panel_colour = "#7f1d1d" if _missing_supplier else "#052e16"
        _supplier_panel_border = "#ef4444" if _missing_supplier else "#22c55e"
        _supplier_panel_text = (
            f"⚠️ Supplier missing on {len(_missing_supplier)} selected line(s). Select supplier before preview/save."
            if _missing_supplier else
            f"Supplier: {', '.join(_selected_suppliers) if _selected_suppliers else '—'}"
        )
        st.markdown(
            f"<div style='background:{_supplier_panel_colour};border:1px solid {_supplier_panel_border};"
            f"border-radius:8px;padding:8px 12px;margin-bottom:8px'>"
            f"<b style='color:#e2e8f0'>{_supplier_panel_text}</b></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='text-align:right;color:#10b981;font-weight:700;"
            f"font-size:0.95rem;margin-bottom:8px'>"
            f"{len(_sel_lines)} line(s) selected · Total: ₹{_total_val:,.2f}</div>",
            unsafe_allow_html=True,
        )

        if not _doc_no.strip():
            st.warning("⚠️ Enter Challan / Invoice No. before previewing.")

        # Save buttons — quick save OR preview first
        if not st.session_state.get("prx_preview_mode"):
            _sv1, _sv2, _sv3 = st.columns([3, 2, 1])
            _disabled = not _doc_no.strip() or bool(_missing_supplier)

            # Quick save — single click, no preview
            if _sv1.button(
                f"💾 Save Purchase ({len(_sel_lines)} line(s) · ₹{_total_val:,.2f})",
                type="primary", use_container_width=True, key="prx_quick_save_btn",
                disabled=_disabled,
            ):
                # Treat as confirmed with current values — skip preview
                st.session_state["prx_preview_mode"] = False
                st.session_state["prx_quick_save"] = True

            # Preview first (for paise edits)
            _do_preview = _sv2.button(
                "👁 Preview & Edit",
                use_container_width=True, key="prx_preview_btn",
                disabled=_disabled,
            )
            if _do_preview and _doc_no.strip():
                st.session_state["prx_preview_mode"] = True
                st.rerun()

            # Quick save triggered — use sel_lines as-is
            if st.session_state.pop("prx_quick_save", False):
                st.session_state["prx_confirmed_lines"] = _sel_lines
                st.rerun()

        # If confirmed via quick save session state
        if st.session_state.get("prx_confirmed_lines") is not None:
            _confirmed_lines = st.session_state.pop("prx_confirmed_lines")
            _sel_lines = _confirmed_lines
            st.session_state["prx_preview_mode"] = False

        else:
            # Preview mode — show editable invoice; returns edited lines on confirm
            _confirmed_lines = _render_invoice_preview(
                _sel_lines, _doc_no, _doc_type, _doc_date, _sup_names
            )
            if _confirmed_lines is not None:
                _sel_lines = _confirmed_lines
                st.session_state["prx_preview_mode"] = False

        # ── Actual DB save ────────────────────────────────────────────────
        # Triggered by: quick save button OR preview confirm button
        _do_save = (
            not st.session_state.get("prx_preview_mode")
            and locals().get("_confirmed_lines") is not None
        )

        if _do_save and _doc_no.strip():
            from modules.core.date_guard import validate_not_future
            _ok_dt, _msg_dt = validate_not_future(_doc_date, "Procurement document date")
            if not _ok_dt:
                st.error(_msg_dt)
                return
            _saved = 0
            _failed = 0
            _stage_failed = 0
            for _item in _sel_lines:
                _ln2  = _item["line"]
                _sid2 = _item["supplier_id"]
                _snm2 = _item["supplier_name"]
                _pr2  = float(_item["price"])
                _q2v  = int(_item["qty"])
                _tot2 = round(_pr2 * _q2v, 2)
                # Combine batch note with invoice path for notes field
                _notes_val = " | ".join(filter(None, [
                    _batch_note.strip() if _batch_note.strip() else "",
                    (_inv_disk_path or _inv_name or "").strip() or
                    ("b64:" + _inv_b64[:20] if _inv_b64 else ""),
                ])) or None

                # ── Product identity (migration 0009 columns) ──────────────
                # Supplier product = what the supplier/lab invoice will say.
                # Our product      = the durable link for billing/inventory.
                # All values come from the line query already in memory — no
                # extra DB lookup. Supplier-product text is built the same way
                # the on-screen "Supplier Product:" line is composed.
                if not str(_sid2 or "").strip():
                    _failed += 1
                    st.error(f"Supplier missing for {_ln2.get('order_no')} / {_ln2.get('eye_side')}. Line not saved.")
                    continue
                _sp_name = str(_item.get("supplier_item") or _ln2.get("supplier_product_name") or "").strip()
                _sp_brand = str(_ln2.get("supplier_brand") or "").strip()
                _sp_index = str(_ln2.get("supplier_index") or "").strip()
                _our_pname = str(_ln2.get("product_name", "")).split(" | ")[0].strip()
                _our_pid = str(_ln2.get("product_id") or "").strip() or None
                # Power text for the supplier description (S/C/AX/ADD)
                _sp_pwr_bits = []
                for _pk, _pl in (("sph", "S"), ("cyl", "C"),
                                 ("axis", "AX"), ("add_power", "ADD")):
                    _pv = _ln2.get(_pk)
                    if _pv and str(_pv) not in ("", "0", "0.0", "None"):
                        try:
                            _pf = float(_pv)
                            if _pk == "cyl" and abs(_pf) < 0.01:
                                continue
                            _sp_pwr_bits.append(
                                f"AX{int(_pf)}" if _pk == "axis"
                                else f"{_pl}{_pf:+.2f}")
                        except Exception:
                            pass
                _sp_desc = " · ".join(filter(None, [
                    _sp_brand, _sp_name,
                    (f"Index {_sp_index}" if _sp_index else ""),
                    (" ".join(_sp_pwr_bits) if _sp_pwr_bits else ""),
                ])) or None
                # mapping_source: if supplier_product_map gave us a name it is
                # a mapped match; otherwise the supplier identity is unknown
                # (left NULL — truthful, ready for later OCR/manual fill).
                _map_src = "supplier_product_map" if _sp_name else None

                # Upsert via explicit EXISTS check — avoids ON CONFLICT (order_line_id)
                # which requires a UNIQUE constraint on order_line_id that may not exist.
                # If the constraint IS added later (recommended), switch back to ON CONFLICT.
                _existing_pa = _q2(
                    "SELECT id::text AS pa_id FROM purchase_acknowledgements "
                    "WHERE order_line_id = %(lid)s::uuid LIMIT 1",
                    {"lid": _ln2["line_id"]},
                )
                if _existing_pa:
                    _ok = _w2("""
                        UPDATE purchase_acknowledgements SET
                            supplier_id   = %(sid)s::uuid,
                            supplier_name = %(snm)s,
                            challan_no    = %(chal)s,
                            invoice_no    = %(inv)s,
                            document_date = %(idate)s::date,
                            notes         = COALESCE(NULLIF(%(notes)s,''), notes),
                            received_qty  = %(qty)s,
                            purchase_price= %(price)s,
                            total_value   = %(total)s,
                            batch_no      = COALESCE(NULLIF(%(batch)s,''), batch_no),
                            expiry_date   = COALESCE(NULLIF(%(expiry)s,'')::date, expiry_date),
                            supplier_product_name        = COALESCE(NULLIF(%(sp_name)s,''), supplier_product_name),
                            supplier_product_code        = COALESCE(NULLIF(%(sp_code)s,''), supplier_product_code),
                            supplier_product_description = COALESCE(NULLIF(%(sp_desc)s,''), supplier_product_description),
                            our_product_name             = COALESCE(NULLIF(%(our_pname)s,''), our_product_name),
                            our_product_id               = COALESCE(NULLIF(%(our_pid)s,'')::uuid, our_product_id),
                            mapping_source               = COALESCE(NULLIF(%(map_src)s,''), mapping_source),
                            billing_status  = 'PURCHASE_ACKED',
                            acknowledged_at = NOW()
                        WHERE order_line_id = %(lid)s::uuid
                    """, {
                        "lid": _ln2["line_id"], "sid": _sid2, "snm": _snm2,
                        "chal": _doc_no.strip() if _doc_type in ("CHALLAN","BOTH") else "",
                        "inv":  _doc_no.strip() if _doc_type in ("INVOICE","BOTH") else "",
                        "idate": str(_doc_date), "notes": _notes_val,
                        "qty": _q2v, "price": _pr2, "total": _tot2,
                        "batch": _item.get("batch_no",""),
                        "expiry": _item.get("expiry_date",""),
                        "sp_name": _sp_name or "", "sp_code": "",
                        "sp_desc": _sp_desc or "", "our_pname": _our_pname or "",
                        "our_pid": _our_pid or "", "map_src": _map_src or "",
                    })
                else:
                    _ok = _w2("""
                        INSERT INTO purchase_acknowledgements (
                            order_line_id, order_no,
                            supplier_id, supplier_name,
                            challan_no, invoice_no, document_date,
                            notes,
                            received_qty, purchase_price, total_value,
                            batch_no, expiry_date,
                            supplier_product_name, supplier_product_code,
                            supplier_product_description,
                            our_product_name, our_product_id, mapping_source,
                            billing_status, acknowledged_at
                        ) VALUES (
                            %(lid)s::uuid, %(ono)s,
                            %(sid)s::uuid, %(snm)s,
                            %(chal)s, %(inv)s, %(idate)s::date,
                            %(notes)s,
                            %(qty)s, %(price)s, %(total)s,
                            %(batch)s, NULLIF(%(expiry)s,'')::date,
                            NULLIF(%(sp_name)s,''), NULLIF(%(sp_code)s,''),
                            NULLIF(%(sp_desc)s,''),
                            NULLIF(%(our_pname)s,''),
                            NULLIF(%(our_pid)s,'')::uuid, NULLIF(%(map_src)s,''),
                            'PURCHASE_ACKED', NOW()
                        )
                    """, {
                    "lid":   _ln2["line_id"],
                    "ono":   _ln2["order_no"],
                    "sid":   _sid2,
                    "snm":   _snm2,
                    "chal":  _doc_no.strip() if _doc_type in ("CHALLAN","BOTH") else "",
                    "inv":   _doc_no.strip() if _doc_type in ("INVOICE","BOTH") else "",
                    "idate": str(_doc_date),
                    "notes": _notes_val,
                    "qty":   _q2v,
                    "price": _pr2,
                    "total": _tot2,
                    "batch": _item.get("batch_no", ""),
                    "expiry": _item.get("expiry_date", ""),
                    "sp_name":  _sp_name or "",
                    "sp_code":  "",          # supplier SKU — not yet sourced;
                                             # OCR/manual will fill later
                    "sp_desc":  _sp_desc or "",
                    "our_pname": _our_pname or "",
                    "our_pid":  _our_pid or "",
                    "map_src":  _map_src or "",
                })
                if _ok:
                    # ── Move line to procurement: update lens_params stage ──
                    try:
                        _lp_done = _ln2.get("lens_params") or {}
                        if isinstance(_lp_done, str):
                            try:
                                _lp_done = json.loads(_lp_done)
                            except Exception:
                                _lp_done = {}
                        _lp_done = dict(_lp_done or {})
                        _route_done = str(_lp_done.get("manufacturing_route") or _ln2.get("route") or "").upper()
                        if _route_done in ("VENDOR", "EXTERNAL_LAB"):
                            _lp_done["supplier_stage"] = "READY_FOR_BILLING"
                        elif _route_done == "STOCK":
                            _lp_done["replenishment_status"] = "PROCURED"
                        else:
                            # Fallback: mark as procured for any other route
                            _lp_done["procurement_status"] = "PROCURED"
                        _stage_ok = _w2(
                            "UPDATE order_lines SET lens_params=%(lp)s::jsonb WHERE id=%(lid)s::uuid",
                            {"lp": json.dumps(_lp_done), "lid": _ln2["line_id"]},
                        )
                        if not _stage_ok:
                            _stage_failed += 1
                    except Exception as _stage_save_err:
                        _stage_failed += 1
                        st.caption(f"Stage sync pending for {_ln2.get('order_no')}: {_stage_save_err}")
                    _saved += 1
                else:
                    _failed += 1
            if _saved:
                try:
                       from modules.procurement.procurement_ledger import record_procurement_receipt
                       record_procurement_receipt(
                           line_items=[
                               {
                                   "line_id": _it["line"]["line_id"],
                                   "qty": _it["qty"],
                                   "price": _it["price"],
                                   "batch_no": _it.get("batch_no", ""),
                                   "expiry_date": _it.get("expiry_date", ""),
                               }
                               for _it in _sel_lines if _it in _sel_lines
                           ],
                           supplier_id=str(_sel_lines[0].get("supplier_id") if _sel_lines else ""),
                           supplier_name=str(_sup_names.get(_sel_lines[0].get("supplier_id") if _sel_lines else "", "")),
                           document_no=_doc_no.strip(),
                           document_type=_doc_type,
                           document_date=str(_doc_date),
                           invoice_file_path=_inv_disk_path or _inv_name or "",
                           source="PRODUCTION_QUEUE",
                       )
                except Exception as _pl_save_err:
                    st.warning(f"Procurement ledger mirror pending: {_pl_save_err}")

                # ── Auto-post Purchase Invoice JV to accounting ───────────
                import decimal as _dec_acc
                def _r2a(v):
                    try: return float(_dec_acc.Decimal(str(v)).quantize(_dec_acc.Decimal("0.01"), rounding=_dec_acc.ROUND_HALF_UP))
                    except: return round(float(v or 0), 2)

                _acc_taxable = sum(_r2a(_it["price"] * _it["qty"]) for _it in _sel_lines)
                _acc_gst     = sum(_r2a(_r2a(_it["price"] * _it["qty"]) * _it.get("gst_percent",0) / 100) for _it in _sel_lines)
                _acc_total   = _r2a(_acc_taxable + _acc_gst)
                # Determine dominant purchase category from lines
                _cats = [str(_it.get("category","LENSES")).upper() for _it in _sel_lines]
                _acc_cat = max(set(_cats), key=_cats.count) if _cats else "LENSES"
                _acc_sup = str(_sel_lines[0].get("supplier_name","")) if _sel_lines else ""
                # Accounting JV — run in background thread with timeout so a
                # stuck/leaked DB connection in accounts_engine never blocks the UI.
                try:
                    import threading as _thr
                    _acc_result = [None, None]
                    def _run_acc_jv():
                        try:
                            from modules.accounting.accounts_engine import post_purchase_invoice_jv
                            _ok, _vno = post_purchase_invoice_jv(
                                invoice_no        = _doc_no.strip(),
                                invoice_id        = "",
                                supplier_name     = _acc_sup,
                                grand_total       = _acc_total,
                                taxable           = _acc_taxable,
                                tax_amount        = _acc_gst,
                                purchase_category = _acc_cat,
                                voucher_date      = _doc_date,
                                created_by        = st.session_state.get("user_name","Staff"),
                            )
                            _acc_result[0], _acc_result[1] = _ok, _vno
                        except Exception as _ae:
                            _acc_result[0], _acc_result[1] = False, str(_ae)
                    _t = _thr.Thread(target=_run_acc_jv, daemon=True)
                    _t.start()
                    _t.join(timeout=8)  # wait max 8 seconds
                    if _t.is_alive():
                        st.caption("⚠️ Accounting post queued (will complete in background)")
                    elif _acc_result[0]:
                        st.caption(f"📒 Journal posted: {_acc_result[1]}  Dr Purchase / Cr {_acc_sup}")
                    else:
                        st.caption(f"⚠️ Accounting post pending: {_acc_result[1]}")
                except Exception as _acc_err:
                    st.caption(f"⚠️ Accounting wiring pending: {_acc_err}")
                _fail_note = f" · {_failed} failed" if _failed else ""
                if _stage_failed:
                    st.warning(
                        f"Saved purchase for {_saved} line(s), but stage sync failed for {_stage_failed}. "
                        "Use DB Check on this page if any line does not appear in Procured."
                    )
                else:
                    st.success(
                        f"✅ Saved as Purchase → moved to Procured: {_saved} line(s){_fail_note} · "
                        f"Supplier: {_acc_sup or '—'} · Ref: {_doc_no.strip()} · "
                        f"₹{sum(_r2_top(i['price']*i['qty']) for i in _sel_lines):,.2f}"
                    )
                # Clear preview mode before rerun — no sleep needed
                st.session_state.pop("prx_preview_mode", None)
                st.session_state.pop("prx_preview_btn",  None)
                # Bust procurement queue cache so saved lines disappear on rerun
                for _k in list(st.session_state.keys()):
                    if _k.startswith("_prx_rows_"):
                        st.session_state.pop(_k, None)
                st.rerun()
            elif _failed:
                st.session_state.pop("prx_preview_mode", None)
                st.error(f"❌ Save failed for {_failed} line(s). Check DB connection and try again.")



# ── Public entry points (called from production_page.py) ──────────────────

def render_procurement_queue():
    """Main entry point for 📥 Procurement Queue tab."""
    _promoted = _auto_promote_po_sent_siblings()
    if _promoted:
        st.success(f"✅ Synced {_promoted} PO_SENT sibling line(s) into Procurement Queue.")
    # ── TEMPORARY read-only diagnostic (safe to remove later) ─────────────
    # Verifies migration 0009 + the 6 new PA columns + what was actually
    # saved for an order. SELECT-only; touches nothing.
    try:
        with st.expander("🔧 PA Identity Diagnostic (read-only — remove after use)", expanded=False):
            _dord = st.text_input("Order No to inspect",
                                   value="R/2627/0121",
                                   key="_pa_diag_ono")
            if st.button("Run diagnostic", key="_pa_diag_run"):
                from modules.backoffice._diag_pa_identity import diag_text
                st.code(diag_text(_dord.strip() or None), language="text")
    except Exception as _diag_e:
        st.caption(f"diagnostic unavailable: {_diag_e}")
    # ── end temporary diagnostic ─────────────────────────────────────────
    _render_procurement_rx()


def render_procurement_analytics():
    """Entry point for 📦 Procurement Analytics tab."""
    _render_procurement_analytics()
