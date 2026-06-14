"""
order_line_adder.py
─────────────────────────────────────────────────────────────────────────────
Adds / mirrors order lines inside backoffice BEFORE the order is saved/confirmed.

Features:
  • + Add Line  — product search → eye side → RX powers → lens_params copy option
  • Mirror LE/RE  — if one eye has colour/fitting, offer to copy to the missing eye
  • Saves directly to order_lines table via INSERT ON CONFLICT
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import uuid
import streamlit as st
from typing import Dict, List


# ── helpers ──────────────────────────────────────────────────────────────────

def _rq(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _write(sql, params):
    """Use run_write for all DML (INSERT/UPDATE/DELETE) — run_query is SELECT-only."""
    try:
        from modules.sql_adapter import run_write
        return run_write(sql, params)
    except Exception as e:
        st.error(f"DB error: {e}")
        return False


def _order_is_editable(order: Dict) -> bool:
    """Editable until backoffice SAVE TO ORDER confirms the order."""
    status = str(order.get("status") or "PENDING").upper()
    return status not in ("CONFIRMED", "BILLED", "DISPATCHED",
                          "DELIVERED", "CLOSED", "CANCELLED")
    # PENDING and UNDER_REVIEW are both editable


def _all_lines(order: Dict) -> List[Dict]:
    lines = []
    for key in ("stock_lines", "inhouse_lines", "lab_order_lines", "lines"):
        lines.extend(order.get(key) or [])
    # deduplicate by line_id
    seen = set()
    out = []
    for l in lines:
        lid = l.get("line_id") or l.get("id") or id(l)
        if lid not in seen:
            seen.add(lid)
            out.append(l)
    return out


# ── Product category helpers ─────────────────────────────────────────────────

def _product_category(main_group: str) -> str:
    """
    Classify a product into one of 4 categories based on main_group string.
    Returns: 'ophthalmic' | 'contact' | 'frame' | 'other'
    """
    mg = (main_group or "").lower()
    if "contact" in mg:
        return "contact"
    if "frame" in mg:
        return "frame"
    if "lens" in mg or "ophthalmic" in mg or "spectacle" in mg:
        return "ophthalmic"
    return "other"


def _ophthalmic_eye_count(order: Dict, product_id: str) -> Dict[str, int]:
    """
    Count existing ophthalmic lines per eye for a given product_id in this order.
    Returns {"R": n, "L": n} — counts active (non-deleted) lines only.
    """
    counts = {"R": 0, "L": 0}
    for ln in _all_lines(order):
        if str(ln.get("product_id") or "") != product_id:
            continue
        eye = str(ln.get("eye_side") or "").upper()
        if eye in counts:
            counts[eye] += 1
    return counts


# ── MIRROR PANEL ─────────────────────────────────────────────────────────────

def render_mirror_panel(order: Dict) -> None:
    """
    Detect if one eye has colour/fitting and the other is missing.
    Offer one-click mirror.
    """
    lines = _all_lines(order)
    by_eye: Dict[str, List[Dict]] = {"R": [], "L": []}
    for ln in lines:
        eye = str(ln.get("eye_side") or "").upper()
        if eye in by_eye:
            by_eye[eye].append(ln)

    # Check each product group — if only one eye exists
    from collections import defaultdict
    by_product: Dict[str, Dict] = defaultdict(dict)
    for ln in lines:
        eye = str(ln.get("eye_side") or "").upper()
        pid = str(ln.get("product_id") or ln.get("product_name") or "")
        if eye in ("R", "L") and pid:
            by_product[pid][eye] = ln

    suggestions = []
    for pid, eyes in by_product.items():
        have = set(eyes.keys())
        # Only flag ophthalmic products (those that have eye_side R or L)
        # Show if EITHER eye is missing
        if len(have) == 1:
            existing_eye = list(have)[0]
            missing_eye  = "L" if existing_eye == "R" else "R"
            ln = eyes[existing_eye]
            lp = ln.get("lens_params") or {}
            if isinstance(lp, str):
                try:
                    import json as _lpj2; lp = _lpj2.loads(lp)
                except: lp = {}
            has_colour  = bool(lp.get("colour") and str(lp["colour"]).lower() not in ("none","no",""))
            has_fitting = bool(lp.get("fitting_required"))
            suggestions.append({
                "pid": pid,
                "pname": ln.get("product_name", pid),
                "have": existing_eye,
                "missing": missing_eye,
                "source_line": ln,
                "has_colour": has_colour,
                "has_fitting": has_fitting,
            })

    if not suggestions:
        return

    # Show alert above expander
    st.markdown(
        f"<div style='background:#1a0f00;border:1px solid #f59e0b;border-radius:8px;"
        f"padding:8px 14px;margin-bottom:6px;display:flex;align-items:center;gap:10px'>"
        f"<span style='font-size:1.2rem'>⚠️</span>"
        f"<span style='color:#fcd34d;font-weight:700;font-size:0.85rem'>"
        f"{len(suggestions)} missing eye line(s) detected — click below to add</span>"
        f"</div>",
        unsafe_allow_html=True)

    with st.expander("⚠️ Fix Missing Eye Lines", expanded=True):
        for s in suggestions:
            _ico = []
            if s["has_colour"]:  _ico.append("🎨")
            if s["has_fitting"]: _ico.append("🔧")
            _ico_str = " ".join(_ico) if _ico else ""

            st.markdown(
                f"<div style='background:#1a1200;border:1px solid #f59e0b44;"
                f"border-radius:8px;padding:10px 14px;margin-bottom:8px'>"
                f"<b style='color:#fcd34d'>{s['pname']}</b>"
                f"<span style='color:#94a3b8;font-size:0.78rem;margin-left:10px'>"
                f"✅ {s['have']} eye exists &nbsp;·&nbsp; "
                f"❌ {s['missing']} eye missing</span>"
                f"{' &nbsp;&nbsp;' + _ico_str if _ico_str else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )

            _m1, _m2, _m3 = st.columns([2, 2, 1])
            with _m1:
                _copy_lp = st.checkbox(
                    f"Copy colour/fitting to {s['missing']}",
                    value=bool(s["has_colour"] or s["has_fitting"]),
                    key=f"mirror_copy_lp_{s['pid']}_{s['missing']}",
                )
            with _m2:
                _mirror_rx = st.checkbox(
                    "Same RX powers",
                    value=False,
                    key=f"mirror_rx_{s['pid']}_{s['missing']}",
                )
            with _m3:
                if st.button(
                    f"➕ Add {s['missing']} Eye",
                    key=f"mirror_btn_{s['pid']}_{s['missing']}",
                    type="primary",
                    use_container_width=True,
                ):
                    _do_mirror(order, s, _copy_lp, _mirror_rx)


def _do_mirror(order: Dict, s: dict, copy_lp: bool, mirror_rx: bool):
    src = s["source_line"]
    order_id = str(order.get("id") or "")
    if not order_id:
        st.error("Order ID missing"); return

    lp = dict(src.get("lens_params") or {}) if copy_lp else {}
    bp = dict(src.get("boxing_params") or {})

    # RX — mirror or blank
    if mirror_rx:
        sph = src.get("sph"); cyl = src.get("cyl")
        axis = src.get("axis"); add = src.get("add_power")
    else:
        sph = cyl = axis = add = None

    new_id = str(uuid.uuid4())
    ok = _write("""
        INSERT INTO order_lines
          (id, order_id, product_id, eye_side,
           sph, cyl, axis, add_power,
           quantity, unit_price, total_price,
           status, lens_params, boxing_params, allocated_qty)
        VALUES
          (%(id)s, %(oid)s::uuid, %(pid)s::uuid, %(eye)s,
           %(sph)s, %(cyl)s, %(axis)s, %(add)s,
           %(qty)s, %(up)s, %(tp)s,
           'PENDING', %(lp)s::jsonb, %(bp)s::jsonb, 0)
        ON CONFLICT (id) DO NOTHING
    """, {
        "id":  new_id,
        "oid": order_id,
        "pid": str(src.get("product_id") or ""),
        "eye": s["missing"],
        "sph": sph, "cyl": cyl, "axis": axis, "add": add,
        "qty": int(src.get("billing_qty") or src.get("quantity") or 1),
        "up":  float(src.get("unit_price") or 0),
        "tp":  float(src.get("billing_total") or src.get("total_price") or 0),
        "lp":  json.dumps(lp),
        "bp":  json.dumps(bp),
    })
    if ok:
        st.success(f"✅ {s['missing']} eye line added for {s['pname']}")
        st.rerun()


# ── ADD LINE PANEL ────────────────────────────────────────────────────────────

def render_add_line_panel(order: Dict) -> None:
    """
    Expander with full add-line form.
    Locked once order is CONFIRMED.
    """
    _status = str(order.get("status") or "PENDING").upper()
    _locked = not _order_is_editable(order)

    order_id = str(order.get("id") or "")
    _add_open_key = f"al_add_open_{order_id}"
    _add_has_work = any(
        bool(st.session_state.get(k))
        for k in (
            f"al_search_{order_id}",
            f"al_product_{order_id}",
            f"al_frame_sku_scan_{order_id}",
            f"al_frame_sku_{order_id}",
        )
    )
    _add_expanded = bool(st.session_state.get(_add_open_key) or _add_has_work)

    with st.expander("➕ Add Line to Order", expanded=_add_expanded):
        if _locked:
            st.warning(
                "⚠️ Order is **CONFIRMED** — adding lines will require re-saving "
                "in backoffice to include them in billing.",
                icon="⚠️")
        st.markdown(
            "<div style='background:#0d1f0d;border-left:3px solid #10b981;"
            "padding:6px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;"
            "color:#94a3b8;font-size:0.78rem'>"
            "Add a missing eye line or any additional product to this order.</div>",
            unsafe_allow_html=True,
        )

        # ── Product search ────────────────────────────────────────────
        from modules.sql_adapter import read_product_master
        _pm = read_product_master()
        if _pm is None or _pm.empty:
            st.warning("Product master unavailable"); return

        _p_opts = {}
        for _, row in _pm.iterrows():
            _pid = str(row.get("product_id") or row.get("id") or "")
            _pn  = str(row.get("product_name") or "")
            _br  = str(row.get("brand") or "")
            _mg  = str(row.get("main_group") or "")
            if _pid and _pn:
                _p_opts[_pid] = f"{_pn}  ·  {_br}  ·  {_mg}"

        _search = st.text_input("🔍 Search product", key=f"al_search_{order_id}",
                                placeholder="Type product name or brand…")
        _filtered = {
            k: v for k, v in _p_opts.items()
            if not _search or _search.lower() in v.lower()
        }
        if not _filtered:
            st.info("No products match search"); return

        _sel_pid = st.selectbox(
            "Product", options=list(_filtered.keys()),
            format_func=lambda x: _filtered.get(x, x),
            key=f"al_product_{order_id}",
        )

        # ── Classify product + enforce ophthalmic 1-pair rule ────────────────
        _existing_lines = _all_lines(order)
        _existing_eyes = {
            str(l.get("eye_side") or "").upper()
            for l in _existing_lines
            if str(l.get("product_id") or "") == _sel_pid
        }

        # Get category from product master
        _sel_row   = _pm[_pm.apply(lambda r: str(r.get("product_id") or r.get("id") or "") == _sel_pid, axis=1)]
        _sel_mg    = str(_sel_row.iloc[0].get("main_group", "") if not _sel_row.empty else "")
        _prod_cat  = _product_category(_sel_mg)
        _eye_counts = _ophthalmic_eye_count(order, _sel_pid)

        # ── Frame: SKU scan + dropdown ───────────────────────────────
        # Frames are tracked per-SKU (each physical frame has a batch_no/SKU).
        # Show a scan box first so staff can scan the barcode on the frame;
        # the dropdown then auto-jumps to the matching SKU.
        # Selected SKU drives both batch_no (stored in boxing_params) and price.
        _sel_frame_sku  = None   # will be set below if frame
        _frame_sku_price = None  # SKU-level price override

        if _prod_cat == "frame":
            try:
                # Fetch all SKU rows for this frame product from inventory_stock.
                # Quantity filter is intentionally relaxed (>= 0) so frames that
                # exist in stock but show qty=0 due to allocation still appear —
                # staff should see and choose the SKU even if qty is borderline.
                _frame_skus = _rq("""
                    SELECT
                        s.batch_no                              AS sku,
                        COALESCE(s.quantity, 0)                 AS qty,
                        COALESCE(s.mrp, 0)                      AS mrp,
                        COALESCE(s.selling_price, s.mrp, 0)     AS selling_price,
                        COALESCE(s.purchase_rate, 0)            AS purchase_rate,
                        COALESCE(s.location, '')                AS location,
                        COALESCE(s.colour_mix, '')              AS colour_mix,
                        COALESCE(s.frame_group, '')             AS frame_group
                    FROM inventory_stock s
                    WHERE s.product_id = %(pid)s::uuid
                      AND COALESCE(s.is_active, TRUE) = TRUE
                      AND s.batch_no IS NOT NULL
                      AND s.batch_no != ''
                    ORDER BY s.batch_no
                """, {"pid": str(_sel_pid)}) or []
            except Exception as _fe:
                _frame_skus = []

            if not _frame_skus:
                st.warning(
                    f"⚠️ No SKUs found for this frame in inventory. "
                    f"Please load stock via the Frame Loader first."
                )
                return

            # Scan / type box
            _fsku_scanned = st.text_input(
                "📷 Scan or type SKU",
                placeholder="e.g. D10007",
                key=f"al_frame_sku_scan_{order_id}",
            ).strip().upper()

            # Build dropdown labels
            _fsku_labels = [
                f"{s['sku']}  |  📍{s['location']}  |  ₹{float(s.get('mrp') or 0):.0f}"
                + (f"  |  {s['colour_mix']}" if s.get("colour_mix") else "")
                + (f"  [{s['frame_group']}]" if s.get("frame_group") else "")
                for s in _frame_skus
            ]

            # Auto-jump dropdown index if scan matches
            _fsku_default = 0
            if _fsku_scanned:
                _fsku_match = next(
                    (i for i, s in enumerate(_frame_skus)
                     if s["sku"].upper() == _fsku_scanned), None
                )
                if _fsku_match is not None:
                    _fsku_default = _fsku_match
                else:
                    st.warning(f"SKU {_fsku_scanned!r} not found in stock for this frame")

            _fsku_sel_label = st.selectbox(
                f"Select SKU ({len(_frame_skus)} in stock)",
                _fsku_labels,
                index=_fsku_default,
                key=f"al_frame_sku_select_{order_id}",
            )
            _fsku_row = _frame_skus[_fsku_labels.index(_fsku_sel_label)]
            _sel_frame_sku   = _fsku_row["sku"]
            _frame_sku_price = float(_fsku_row.get("selling_price") or _fsku_row.get("mrp") or 0)

            st.success(
                f"🕶️ {_p_opts.get(_sel_pid, '').split('  ·  ')[0].strip()} | "
                f"SKU: {_sel_frame_sku} | "
                f"📍{_fsku_row.get('location', '')} | "
                f"₹{float(_fsku_row.get('mrp') or 0):.0f}"
            )

        # ── Eye side ─────────────────────────────────────────────────
        if _prod_cat == "ophthalmic":
            # 1-pair rule: max 1 R + 1 L per order per product
            _r_full = _eye_counts["R"] >= 1
            _l_full = _eye_counts["L"] >= 1

            if _r_full and _l_full:
                st.warning(
                    "Ophthalmic lens pair already complete. "
                    "This order already has 1 R eye and 1 L eye line for this product. "
                    "One job card = one R+L pair. To change RX, edit the existing lines above.")
                return   # stop — nothing more to render

            # Build only the allowed options
            _eye_opts = []
            if not _r_full:
                _eye_opts.append("R")
            if not _l_full:
                _eye_opts.append("L")
            if not _r_full and not _l_full:
                _eye_opts.append("Both (R+L)")

            # Show info about the constraint
            st.info(
                f"👓 **Ophthalmic lens** — 1 pair per order (1 R + 1 L line).  "
                f"{'R eye done · adding L' if _r_full else 'L eye done · adding R' if _l_full else 'Adding pair'}"
            )
        else:
            # Frames, Contact Lenses, Others — unrestricted
            _eye_opts = ["R", "L", "Both (R+L)", "No Eye (Accessory)"]
            _cat_label = {
                "contact": "👁 Contact Lens",
                "frame":   "🕶️ Frame",
                "other":   "📦 Accessory / Other",
            }.get(_prod_cat, "")
            if _cat_label:
                st.caption(_cat_label + " — no pair restriction")

        if not _eye_opts:
            return

        # Suggest the missing eye as default
        if "R" in _existing_eyes and "L" not in _existing_eyes and "L" in _eye_opts:
            _eye_default = _eye_opts.index("L")
        elif "L" in _existing_eyes and "R" not in _existing_eyes and "R" in _eye_opts:
            _eye_default = _eye_opts.index("R")
        else:
            _eye_default = 0
        _sel_eye = st.selectbox("Eye Side", _eye_opts, index=_eye_default,
                                key=f"al_eye_{order_id}")

        # ── RX Powers ────────────────────────────────────────────────
        # If adding missing eye, offer to copy RX from existing eye
        _copy_rx_from = None
        if _sel_eye in ("R", "L") and _existing_eyes:
            _src_eye = list(_existing_eyes)[0]
            _src_line = next(
                (l for l in _existing_lines
                 if str(l.get("product_id") or "") == _sel_pid
                 and str(l.get("eye_side") or "").upper() == _src_eye), None
            )
            if _src_line:
                _copy_rx = st.checkbox(
                    f"Copy RX from {_src_eye} eye",
                    key=f"al_copy_rx_{order_id}",
                )
                if _copy_rx:
                    _copy_rx_from = _src_line

        _pr1, _pr2, _pr3, _pr4 = st.columns(4)
        with _pr1:
            _sph = st.number_input("SPH", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("sph") or 0) if _copy_rx_from else 0.0,
                                   key=f"al_sph_{order_id}")
        with _pr2:
            _cyl = st.number_input("CYL", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("cyl") or 0) if _copy_rx_from else 0.0,
                                   key=f"al_cyl_{order_id}")
        with _pr3:
            _axis = st.number_input("AXIS", min_value=0, max_value=180, step=1,
                                    value=int(_copy_rx_from.get("axis") or 0) if _copy_rx_from else 0,
                                    key=f"al_axis_{order_id}")
        with _pr4:
            _add = st.number_input("ADD", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("add_power") or 0) if _copy_rx_from else 0.0,
                                   key=f"al_add_{order_id}")

        # ── Copy lens_params ─────────────────────────────────────────
        _lp_src = None
        if _existing_eyes:
            _src_eye2 = list(_existing_eyes)[0]
            _lp_line  = next(
                (l for l in _existing_lines
                 if str(l.get("product_id") or "") == _sel_pid
                 and str(l.get("eye_side") or "").upper() == _src_eye2), None
            )
            if _lp_line:
                _src_lp = _lp_line.get("lens_params") or {}
                _has_lp = any([
                    _src_lp.get("colour") and str(_src_lp["colour"]).lower() not in ("none",""),
                    _src_lp.get("fitting_required"),
                    _src_lp.get("instructions"),
                ])
                if _has_lp:
                    if st.checkbox(
                        f"Copy colour/fitting/instructions from {_src_eye2} eye",
                        value=True,
                        key=f"al_copy_lp_{order_id}",
                    ):
                        _lp_src = _src_lp

        # ── Qty + Price ───────────────────────────────────────────────
        # Frames: use SKU-level price from inventory_stock (set above).
        # Others: auto-fill from last order price.
        if _frame_sku_price is not None:
            _suggested_price = _frame_sku_price
        else:
            _price_key = f"al_up_val_{order_id}_{_sel_pid}"
            if _price_key not in st.session_state:
                try:
                    from modules.sql_adapter import fetch_last_product_price
                    st.session_state[_price_key] = fetch_last_product_price(_sel_pid)
                except Exception:
                    st.session_state[_price_key] = 0.0
            _suggested_price = float(st.session_state.get(_price_key) or 0.0)

        _qa, _qb = st.columns(2)
        with _qa:
            _qty = st.number_input("Qty", min_value=1, value=1,
                                   key=f"al_qty_{order_id}")
        with _qb:
            _up = st.number_input("Unit Price ₹", min_value=0.0, step=10.0,
                                  value=_suggested_price,
                                  key=f"al_up_{order_id}",
                                  help="Auto-filled from last sale price")

        # ── Route ────────────────────────────────────────────────────
        _route_opts = ["STOCK", "VENDOR", "INHOUSE", "EXTERNAL_LAB"]
        _sel_route = st.selectbox("Route", _route_opts, key=f"al_route_{order_id}")

        # ── Submit ───────────────────────────────────────────────────
        if st.button("➕ Add to Order", type="primary", use_container_width=True,
                     key=f"al_submit_{order_id}"):
            _eyes_to_add = []
            if _sel_eye == "Both (R+L)":
                _eyes_to_add = ["R", "L"]
            elif _sel_eye == "No Eye (Accessory)":
                _eyes_to_add = [None]
            else:
                _eyes_to_add = [_sel_eye]

            _lp_dict = dict(_lp_src) if _lp_src else {}
            _lp_dict["manufacturing_route"] = _sel_route

            # For frames, store the selected SKU (batch_no) in boxing_params
            # so stock deduction and challan printing can reference the exact unit.
            _bp_dict = {"batch_no": _sel_frame_sku} if _sel_frame_sku else {}
            _tp = float(_up) * int(_qty)

            # Compute discount before inserting
            _disc_pct = 0.0
            _disc_amt = 0.0
            _net_tp   = _tp
            _tmp_line = {}
            try:
                from modules.pricing.discount_flow import apply_order_discounts
                _otype_add = str(order.get("order_type","WHOLESALE"))
                _pid_add   = str(order.get("party_id") or "").strip()
                if not _pid_add:
                    _pname_add = str(order.get("party_name","") or order.get("patient_name","")).strip()
                    if _pname_add:
                        from modules.sql_adapter import run_query as _rq_add
                        _padd = _rq_add("SELECT id::text AS id FROM parties "
                                        "WHERE party_name=%s AND COALESCE(is_active,TRUE)=TRUE LIMIT 1",
                                        (_pname_add,)) or []
                        if _padd: _pid_add = _padd[0].get("id","")
                _tmp_line = {
                    "product_id": str(_sel_pid),
                    "product_name": str(_sel_row.iloc[0].get("product_name", "") if not _sel_row.empty else ""),
                    "brand": str(_sel_row.iloc[0].get("brand", "") if not _sel_row.empty else ""),
                    "main_group": str(_sel_row.iloc[0].get("main_group", "") if not _sel_row.empty else ""),
                    "unit_price": float(_up),
                    "billing_qty": int(_qty),
                    "quantity": int(_qty),
                    "gst_percent": float(_sel_row.iloc[0].get("gst_percent", 0) if not _sel_row.empty else 0),
                    "lens_params": dict(_lp_dict),
                }
                apply_order_discounts([_tmp_line], party_id=_pid_add, order_type=_otype_add)
                _disc_pct = float(_tmp_line.get("discount_percent", 0))
                _disc_amt = float(_tmp_line.get("discount_amount", 0))
                _net_tp   = round(float(_tmp_line.get("billing_total") or _tmp_line.get("total_price") or (_tp - _disc_amt)), 2)
            except Exception:
                pass

            _added = 0
            _new_lines_for_sync = []
            for _eye in _eyes_to_add:
                _new_id = str(uuid.uuid4())
                _ok = _write("""
                    INSERT INTO order_lines
                      (id, order_id, product_id, eye_side,
                       sph, cyl, axis, add_power,
                       quantity, unit_price, total_price,
                       discount_percent, discount_amount, billing_total,
                       applied_rule_ids,
                       status, lens_params, boxing_params, allocated_qty)
                    VALUES
                      (%(id)s, %(oid)s::uuid, %(pid)s::uuid, %(eye)s,
                       %(sph)s, %(cyl)s, %(axis)s, %(add)s,
                       %(qty)s, %(up)s, %(tp)s,
                       %(dp)s, %(da)s, %(bt)s,
                       %(ari)s,
                       'PENDING', %(lp)s::jsonb, %(bp)s::jsonb, 0)
                    ON CONFLICT (id) DO NOTHING
                """, {
                    "id":  _new_id,
                    "oid": order_id,
                    "pid": _sel_pid,
                    "eye": _eye,
                    "sph": float(_sph) if _sph else None,
                    "cyl": float(_cyl) if _cyl else None,
                    "axis": int(_axis) if _axis else None,
                    "add": float(_add) if _add else None,
                    "qty": int(_qty),
                    "up":  float(_up),
                    "tp":  _net_tp,
                    "dp":  _disc_pct,
                    "da":  _disc_amt,
                    "bt":  _net_tp,
                    "ari": str(_tmp_line.get("applied_rule_ids") or ""),
                    "lp":  json.dumps(_lp_dict),
                    "bp":  json.dumps(_bp_dict),
                })
                if _ok:
                    _added += 1
                    _line_copy = dict(_tmp_line)
                    _line_copy.update({
                        "id": _new_id,
                        "line_id": _new_id,
                        "product_id": str(_sel_pid),
                        "eye_side": _eye,
                        "quantity": int(_qty),
                        "billing_qty": int(_qty),
                        "unit_price": float(_up),
                        "discount_percent": _disc_pct,
                        "discount_amount": _disc_amt,
                        "billing_total": _net_tp,
                        "total_price": _net_tp,
                        "lens_params": dict(_lp_dict),
                    })
                    _new_lines_for_sync.append(_line_copy)

            if _added:
                try:
                    order.setdefault("lines", []).extend(_new_lines_for_sync)
                    from modules.backoffice.backoffice_helpers import refresh_order_pricing_rules
                    refresh_order_pricing_rules(order, persist=True)
                except Exception:
                    pass
                st.session_state[_add_open_key] = False
                st.success(f"✅ {_added} line(s) added to order")
                st.rerun()


# ── LINE DELETE PANEL ────────────────────────────────────────────────────────

def render_line_delete_panel(order: Dict) -> None:
    """
    Shows existing order lines with a delete button on each.
    Used in post-confirm view on retail/wholesale punching screens.
    Guards:
      - billed_qty > 0  → 🔒 show lock, no delete
      - soft-delete: sets is_deleted = TRUE (never hard-deletes)
    """
    lines = _all_lines(order)
    if not lines:
        return

    # Filter out already-deleted lines (safety)
    lines = [l for l in lines if not l.get("is_deleted")]
    if not lines:
        return

    with st.expander(f"🗑️ Remove a Line  ({len(lines)} line{'s' if len(lines) != 1 else ''})", expanded=False):
        st.markdown(
            "<div style='background:#1a0505;border-left:3px solid #ef4444;"
            "padding:6px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;"
            "color:#94a3b8;font-size:0.78rem'>"
            "Remove a line added by mistake. Billed lines are locked.</div>",
            unsafe_allow_html=True,
        )

        for ln in lines:
            _lid   = str(ln.get("line_id") or ln.get("id") or "")
            _eye   = str(ln.get("eye_side") or "—")
            _pn    = ln.get("product_name") or "—"
            _br    = ln.get("brand") or ""
            _bqty  = int(ln.get("billed_qty") or 0)
            _qty   = int(ln.get("billing_qty") or ln.get("quantity") or 0)
            _price = float(ln.get("unit_price") or 0)

            _eye_col = "#4ade80" if _eye == "R" else "#60a5fa" if _eye == "L" else "#94a3b8"

            _rc1, _rc2, _rc3 = st.columns([0.5, 4, 1])
            with _rc1:
                st.markdown(
                    f"<div style='background:{_eye_col}22;color:{_eye_col};"
                    f"font-weight:900;text-align:center;padding:6px 4px;"
                    f"border-radius:6px;font-size:0.9rem'>{_eye}</div>",
                    unsafe_allow_html=True)
            with _rc2:
                st.markdown(
                    f"<div style='color:#e2e8f0;font-weight:700;font-size:0.85rem'>{_pn}</div>"
                    f"<div style='color:#64748b;font-size:0.68rem'>{_br}"
                    f"  ·  Qty {_qty}  ·  ₹{_price:,.2f}</div>",
                    unsafe_allow_html=True)
            with _rc3:
                if _bqty > 0:
                    st.markdown(
                        "<div style='text-align:center;padding:6px 0'>"
                        "<span style='font-size:1rem' title='Already billed — use Credit Note'>🔒</span>"
                        "</div>",
                        unsafe_allow_html=True)
                else:
                    _del_key = f"al_del_confirm_{_lid}"
                    if st.session_state.get(_del_key):
                        _dy, _dn = st.columns(2)
                        with _dy:
                            if st.button("✅", key=f"al_del_yes_{_lid}",
                                         use_container_width=True, help="Confirm delete"):
                                ok = _write("""
                                    UPDATE order_lines
                                    SET is_deleted = TRUE,
                                        deleted_at = NOW(),
                                        deleted_by = 'punching_edit'
                                    WHERE id = %(lid)s::uuid
                                      AND COALESCE(billed_qty, 0) = 0
                                """, {"lid": _lid})
                                st.session_state.pop(_del_key, None)
                                if ok:
                                    st.success("🗑️ Removed")
                                    st.rerun()
                        with _dn:
                            if st.button("❌", key=f"al_del_no_{_lid}",
                                         use_container_width=True, help="Cancel"):
                                st.session_state.pop(_del_key, None)
                                st.rerun()
                    else:
                        # Block delete if it would leave 0 active lines
                        _active_count = sum(
                            1 for l in lines
                            if not l.get("is_deleted")
                            and int(l.get("billed_qty") or 0) == 0
                        )
                        _last_line = (_active_count <= 1)
                        if _last_line:
                            # Warn but still allow — user may be replacing with Add Line
                            _last_confirm_key = f"al_del_last_confirm_{_lid}"
                            if not st.session_state.get(_last_confirm_key):
                                if st.button("🗑️", key=f"al_del_btn_{_lid}",
                                             use_container_width=True,
                                             help="Last line — click to confirm removal"):
                                    st.session_state[_last_confirm_key] = True
                                    st.rerun()
                                st.caption("⚠️ Last line")
                            else:
                                st.warning("Remove last line? Order will be empty.")
                                _cya, _cyn = st.columns(2)
                                with _cya:
                                    if st.button("✅ Yes", key=f"al_del_last_yes_{_lid}",
                                                 use_container_width=True, type="primary"):
                                        st.session_state.pop(_last_confirm_key, None)
                                        st.session_state[_del_key] = True
                                        st.rerun()
                                with _cyn:
                                    if st.button("✕ No", key=f"al_del_last_no_{_lid}",
                                                 use_container_width=True):
                                        st.session_state.pop(_last_confirm_key, None)
                                        st.rerun()
                        else:
                            if st.button("🗑️", key=f"al_del_btn_{_lid}",
                                         use_container_width=True, help="Remove line"):
                                st.session_state[_del_key] = True
                                st.rerun()

            st.markdown("<div style='border-top:1px solid #1e293b;margin:4px 0'></div>",
                        unsafe_allow_html=True)

        # ── Reset entire order (remove all non-billed lines) ──────────────
        _deletable = [l for l in lines if not int(l.get("billed_qty") or 0)]
        if len(_deletable) > 1:
            st.markdown("<div style='border-top:2px solid #ef4444;margin:8px 0 6px'></div>",
                        unsafe_allow_html=True)
            _reset_key = f"al_reset_all_{order.get('id','')[:8]}"
            if st.session_state.get(_reset_key):
                st.warning(f"⚠️ This will remove all {len(_deletable)} unbilled lines. Are you sure?")
                _ry, _rn = st.columns(2)
                with _ry:
                    if st.button("✅ Yes, Reset Order",
                                 key=f"al_reset_yes_{order.get('id','')[:8]}",
                                 use_container_width=True,
                                 type="primary"):
                        _ids = [str(l.get("line_id") or l.get("id","")) for l in _deletable]
                        for _lid2 in _ids:
                            _write("""
                                UPDATE order_lines
                                SET is_deleted = TRUE,
                                    deleted_at = NOW(),
                                    deleted_by = 'punching_reset'
                                WHERE id = %(lid)s::uuid
                                  AND COALESCE(billed_qty, 0) = 0
                            """, {"lid": _lid2})
                        st.session_state.pop(_reset_key, None)
                        st.success(f"✅ {len(_ids)} line(s) removed — order reset")
                        st.rerun()
                with _rn:
                    if st.button("❌ Cancel",
                                 key=f"al_reset_no_{order.get('id','')[:8]}",
                                 use_container_width=True):
                        st.session_state.pop(_reset_key, None)
                        st.rerun()
            else:
                if st.button("🔄 Reset Entire Order (remove all unbilled lines)",
                             key=_reset_key + "_btn",
                             use_container_width=True,
                             help="Removes all lines not yet billed — use when R/L powers were wrong"):
                    st.session_state[_reset_key] = True
                    st.rerun()


# ── PUNCHED ORDER EDIT VIEW ──────────────────────────────────────────────────

def render_order_edit_sidebar():
    """
    Sidebar widget showing recent punched orders.
    Opens a simple edit view for orders that are still editable
    (PENDING / not yet backoffice-confirmed).
    """
    _orders = _rq("""
        SELECT o.id, o.order_no, o.patient_name, o.party_name,
               o.order_type, o.status, o.created_at,
               COUNT(ol.id) AS line_count
        FROM orders o
        LEFT JOIN order_lines ol ON ol.order_id = o.id
          AND COALESCE(ol.is_deleted, FALSE) = FALSE
        WHERE o.status NOT IN ('CLOSED','CANCELLED','DELIVERED')
          AND o.created_at >= NOW() - INTERVAL '30 days'
        GROUP BY o.id, o.order_no, o.patient_name, o.party_name,
                 o.order_type, o.status, o.created_at
        ORDER BY o.created_at DESC
        LIMIT 40
    """)

    if not _orders:
        st.sidebar.caption("No recent orders")
        return

    # Group: editable vs confirmed
    _editable   = [o for o in _orders if str(o.get("status","")).upper()
                   not in ("CONFIRMED","BILLED","DISPATCHED","DELIVERED","CLOSED")]
    _confirmed  = [o for o in _orders if str(o.get("status","")).upper()
                   in ("CONFIRMED","BILLED","READY","READY_FOR_BILLING")]

    with st.sidebar:
        st.markdown("---")
        st.markdown(
            "<div style='color:#60a5fa;font-size:0.7rem;font-weight:700;"
            "letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px'>"
            "📋 Recent Orders</div>",
            unsafe_allow_html=True,
        )

        _tab_e, _tab_c = st.tabs(["✏️ Editable", "✅ Confirmed"])

        with _tab_e:
            if not _editable:
                st.caption("No editable orders")
            for o in _editable[:15]:
                _ono  = o.get("order_no","—")
                _name = o.get("patient_name") or o.get("party_name") or "—"
                _lc   = int(o.get("line_count") or 0)
                _otype = str(o.get("order_type") or "")[:1]
                if st.button(
                    f"✏️ {_ono}",
                    key=f"edit_order_{o['id']}",
                    use_container_width=True,
                    help=f"{_name} · {_lc} lines · {_otype}",
                ):
                    st.session_state["edit_order_id"] = str(o["id"])
                    st.session_state["edit_order_no"] = _ono
                    st.rerun()
                st.caption(f"{_name} · {_lc} lines")

        with _tab_c:
            if not _confirmed:
                st.caption("No confirmed orders")
            for o in _confirmed[:15]:
                _ono  = o.get("order_no","—")
                _name = o.get("patient_name") or o.get("party_name") or "—"
                try:
                    from modules.backoffice.order_status_live import STATUS_META as _osl
                    _sc = _osl.get(str(o.get("status","")).upper(), {}).get("color","#64748b")
                except Exception:
                    _sc = {"CONFIRMED":"#6366f1","BILLED":"#059669",
                           "READY":"#10b981","READY_FOR_BILLING":"#f59e0b"}.get(
                           str(o.get("status","")).upper(), "#64748b")
                st.markdown(
                    f"<div style='background:#0f172a;border:1px solid #1e293b;"
                    f"border-radius:6px;padding:5px 10px;margin-bottom:4px'>"
                    f"<div style='color:#e2e8f0;font-size:0.78rem;font-weight:700'>{_ono}</div>"
                    f"<div style='color:#64748b;font-size:0.65rem'>{_name}</div>"
                    f"<span style='color:{_sc};font-size:0.6rem;font-weight:700'>"
                    f"{o.get('status','')}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )


# ── CART LINE ADDER (session-state mode for punching screens) ─────────────────
# Used by retail_punching and wholesale_punching BEFORE order is saved to DB.
# Adds a new line dict directly into st.session_state.retail_order_lines.

def render_cart_line_adder(order_type: str = "RETAIL") -> None:
    """
    ➕ Add Line panel for use inside retail/wholesale punching carts.

    Works entirely in session state — no DB writes.
    The line is appended to st.session_state.retail_order_lines with the same
    structure as lines produced by the punching allocation flow.

    order_type: "RETAIL" or "WHOLESALE" — affects GST label only.
    """
    import uuid as _uuid
    import datetime as _dt

    with st.expander("➕ Add Extra Line to Cart", expanded=False):
        st.markdown(
            "<div style='background:#0d1f0d;border-left:3px solid #10b981;"
            "padding:6px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;"
            "color:#94a3b8;font-size:0.78rem'>"
            "Add a missing eye line or any extra product before submitting the order.</div>",
            unsafe_allow_html=True,
        )

        # ── Product search ────────────────────────────────────────────────
        try:
            from modules.sql_adapter import read_product_master
            _pm = read_product_master()
        except Exception:
            st.warning("Product master unavailable")
            return

        if _pm is None or _pm.empty:
            st.warning("Product master unavailable")
            return

        _p_opts: Dict[str, str] = {}
        _p_data: Dict[str, dict] = {}
        for _, row in _pm.iterrows():
            _pid = str(row.get("product_id") or row.get("id") or "")
            _pn  = str(row.get("product_name") or "")
            _br  = str(row.get("brand") or "")
            _mg  = str(row.get("main_group") or "")
            if _pid and _pn:
                _p_opts[_pid] = f"{_pn}  ·  {_br}  ·  {_mg}"
                _p_data[_pid] = dict(row)

        _search = st.text_input(
            "🔍 Search product",
            key="cal_search",
            placeholder="Type product name or brand…"
        )
        _filtered = {k: v for k, v in _p_opts.items()
                     if not _search or _search.lower() in v.lower()}
        if not _filtered:
            st.info("No products match search")
            return

        _sel_pid = st.selectbox(
            "Product",
            options=list(_filtered.keys()),
            format_func=lambda x: _filtered.get(x, x),
            key="cal_product",
        )
        _prow = _p_data.get(_sel_pid, {})

        # ── Eye side ──────────────────────────────────────────────────────
        _existing_eyes = {
            str(l.get("eye_side") or "").upper()
            for l in (st.session_state.get("retail_order_lines") or [])
            if str(l.get("product_id") or "") == _sel_pid
        }
        _eye_opts = ["R", "L", "Both (R+L)", "No Eye (Accessory)"]
        if "R" in _existing_eyes and "L" not in _existing_eyes:
            _eye_default = 1
        elif "L" in _existing_eyes and "R" not in _existing_eyes:
            _eye_default = 0
        else:
            _eye_default = 0

        _sel_eye = st.selectbox("Eye Side", _eye_opts, index=_eye_default, key="cal_eye")

        # ── Copy RX from existing line ────────────────────────────────────
        _copy_rx_from = None
        if _sel_eye in ("R", "L") and _existing_eyes:
            _src_eye = list(_existing_eyes)[0]
            _src_line = next(
                (l for l in (st.session_state.get("retail_order_lines") or [])
                 if str(l.get("product_id") or "") == _sel_pid
                 and str(l.get("eye_side") or "").upper() == _src_eye),
                None
            )
            if _src_line:
                if st.checkbox(f"Copy RX from {_src_eye} eye", key="cal_copy_rx"):
                    _copy_rx_from = _src_line

        _pr1, _pr2, _pr3, _pr4 = st.columns(4)
        with _pr1:
            _sph = st.number_input("SPH", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("sph") or 0) if _copy_rx_from else 0.0,
                                   key="cal_sph")
        with _pr2:
            _cyl = st.number_input("CYL", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("cyl") or 0) if _copy_rx_from else 0.0,
                                   key="cal_cyl")
        with _pr3:
            _axis = st.number_input("AXIS", min_value=0, max_value=180, step=1,
                                    value=int(_copy_rx_from.get("axis") or 0) if _copy_rx_from else 0,
                                    key="cal_axis")
        with _pr4:
            _add = st.number_input("ADD", step=0.25, format="%.2f",
                                   value=float(_copy_rx_from.get("add_power") or 0) if _copy_rx_from else 0.0,
                                   key="cal_add")

        # ── Copy lens_params ──────────────────────────────────────────────
        _lp_src = None
        if _existing_eyes:
            _src_eye2 = list(_existing_eyes)[0]
            _lp_line  = next(
                (l for l in (st.session_state.get("retail_order_lines") or [])
                 if str(l.get("product_id") or "") == _sel_pid
                 and str(l.get("eye_side") or "").upper() == _src_eye2),
                None
            )
            if _lp_line:
                _src_lp = _lp_line.get("lens_params") or {}
                _has_lp = any([
                    _src_lp.get("colour") and str(_src_lp["colour"]).lower() not in ("none",""),
                    _src_lp.get("fitting_required"),
                    _src_lp.get("instructions"),
                ])
                if _has_lp and st.checkbox(
                    f"Copy colour/fitting from {_src_eye2} eye",
                    value=True, key="cal_copy_lp"
                ):
                    _lp_src = _src_lp

        # ── Qty + Price ───────────────────────────────────────────────────
        _price_key = f"cal_price_{_sel_pid}"
        if _price_key not in st.session_state:
            try:
                from modules.sql_adapter import fetch_last_product_price
                st.session_state[_price_key] = fetch_last_product_price(_sel_pid)
            except Exception:
                st.session_state[_price_key] = 0.0

        _qa, _qb, _qc = st.columns(3)
        with _qa:
            _qty = st.number_input("Qty", min_value=1, value=1, key="cal_qty")
        with _qb:
            _up  = st.number_input("Unit Price ₹", min_value=0.0, step=10.0,
                                   value=float(st.session_state.get(_price_key) or 0.0),
                                   key="cal_up", help="Auto-filled from last sale")
        with _qc:
            _route_opts = ["STOCK", "VENDOR", "INHOUSE", "EXTERNAL_LAB"]
            _sel_route = st.selectbox("Route", _route_opts, key="cal_route")

        # ── Add button ────────────────────────────────────────────────────
        if st.button("➕ Add to Cart", type="primary",
                     use_container_width=True, key="cal_submit"):

            _eyes_to_add = (
                ["R", "L"] if _sel_eye == "Both (R+L)"
                else [None] if _sel_eye == "No Eye (Accessory)"
                else [_sel_eye]
            )

            _lp_dict = dict(_lp_src) if _lp_src else {}
            _lp_dict["manufacturing_route"] = _sel_route

            _box_size = max(1, int(_prow.get("box_size") or 1))
            _unit     = str(_prow.get("unit") or "PCS").upper()
            _gst_pct  = float(_prow.get("gst_percent") or 0)

            _added = 0
            for _eye in _eyes_to_add:
                _tp = round(float(_up) * int(_qty), 2)
                _new_line = {
                    "line_id":            str(_uuid.uuid4()),
                    "provisional_order_id": st.session_state.get("retail_provisional_order_id", ""),
                    "product_id":         _sel_pid,
                    "product_name":       _prow.get("product_name", ""),
                    "brand":              _prow.get("brand", ""),
                    "main_group":         _prow.get("main_group", ""),
                    "eye_side":           _eye or "OTHER",
                    "sph":                float(_sph) if _sph else None,
                    "cyl":                float(_cyl) if _cyl else None,
                    "axis":               int(_axis) if _axis else None,
                    "add_power":          float(_add) if _add else None,
                    "lens_params":        _lp_dict,
                    "boxing_params":      {},
                    "requested_qty":      int(_qty),
                    "billing_qty":        int(_qty),
                    "order_qty":          0,
                    "display_qty":        str(_qty),
                    "batch_allocation":   [],
                    "suggested_allocation": [],
                    "unit_price":         float(_up),
                    "total_price":        _tp,
                    "unit":               _unit,
                    "box_size":           _box_size,
                    "gst_percent":        _gst_pct,
                    "gst_amount":         0.0,
                    "discount_percent":   0.0,  # stamped by _stamp_cart_line_discount below
                    "status":             "Complete",
                    "created_at":         _dt.datetime.now().isoformat(),
                    "manufacturing_route": _sel_route,
                }

                if "retail_order_lines" not in st.session_state:
                    st.session_state.retail_order_lines = []
                st.session_state.retail_order_lines.append(_new_line)
                _added += 1

            if _added:
                # clear price cache so next product starts fresh
                st.session_state.pop(_price_key, None)
                st.success(f"✅ {_added} line(s) added to cart")
                st.rerun()
