"""
modules/backoffice/supplier_product_map_ui.py
===============================================
Supplier Product Mapping — maps OUR products to SUPPLIER/LAB product names.
"""
from __future__ import annotations
from typing import Optional, List
import streamlit as st


def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception:
        return []


def _w(sql, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {})
        return True
    except Exception as _e:
        st.error(f"Save error: {_e}")
        return False


def _ensure_supplier_mapping_schema() -> None:
    """Small compatibility patch: treatment is part of supplier mapping UX."""
    try:
        from modules.sql_adapter import run_write
        run_write(
            "ALTER TABLE product_supplier_map "
            "ADD COLUMN IF NOT EXISTS supplier_treatment TEXT"
        )
    except Exception:
        pass


_ensure_supplier_mapping_schema()


# ── Core lookup ───────────────────────────────────────────────────────────────

def _upsert_supplier_mapping(
    product_id: str,
    supplier_id: str,
    supplier_product_name: str,
    supplier_brand: str = "",
    supplier_index: str = "",
    supplier_coating: str = "",
    supplier_treatment: str = "",
    route_type: str = "EXTERNAL_LAB",
    notes: str = "",
) -> bool:
    """Save/update supplier-facing product mapping in existing map table."""
    if not product_id or not supplier_id or not supplier_product_name:
        return False
    import uuid as _uu
    return _w("""
        INSERT INTO product_supplier_map
            (id, product_id, supplier_id, rank,
             supplier_product_name, supplier_brand,
             supplier_index, supplier_coating, supplier_treatment,
             notes, route_type, is_active, created_at, updated_at)
        VALUES
            (%(id)s::uuid, %(pid)s::uuid, %(sid)s::uuid, 1,
             %(spn)s, %(sb)s, %(si)s, %(sc)s, %(st)s,
             %(notes)s, %(route)s, TRUE, NOW(), NOW())
        ON CONFLICT (product_id, supplier_id) WHERE is_active = TRUE
        DO UPDATE SET
            supplier_product_name = EXCLUDED.supplier_product_name,
            supplier_brand        = EXCLUDED.supplier_brand,
            supplier_index        = EXCLUDED.supplier_index,
            supplier_coating      = EXCLUDED.supplier_coating,
            supplier_treatment    = EXCLUDED.supplier_treatment,
            notes                 = EXCLUDED.notes,
            updated_at            = NOW()
    """, {
        "id": str(_uu.uuid4()),
        "pid": product_id,
        "sid": supplier_id,
        "spn": str(supplier_product_name or "").strip(),
        "sb": str(supplier_brand or "").strip(),
        "si": str(supplier_index or "").strip(),
        "sc": str(supplier_coating or "").strip(),
        "st": str(supplier_treatment or "").strip(),
        "notes": str(notes or "").strip(),
        "route": route_type or "EXTERNAL_LAB",
    })

def get_supplier_product_name(product_id: str, supplier_id: str) -> dict:
    """Return supplier's catalogue name for our product."""
    if not product_id or not supplier_id:
        return {"supplier_product_name": "", "supplier_brand": "",
                "supplier_index": "", "supplier_coating": "",
                "supplier_treatment": "", "mapped": False}

    rows = _q("""
        SELECT
            COALESCE(psm.supplier_product_name, '') AS supplier_product_name,
            COALESCE(psm.supplier_brand,         '') AS supplier_brand,
            COALESCE(psm.supplier_index,         '') AS supplier_index,
            COALESCE(psm.supplier_coating,       '') AS supplier_coating,
            COALESCE(psm.supplier_treatment,     '') AS supplier_treatment
        FROM product_supplier_map psm
        WHERE psm.product_id  = %(pid)s::uuid
          AND psm.supplier_id = %(sid)s::uuid
          AND psm.is_active   = TRUE
        ORDER BY psm.rank ASC
        LIMIT 1
    """, {"pid": product_id, "sid": supplier_id})

    if rows:
        r = rows[0]
        _sname = str(r.get("supplier_product_name") or "").strip()
        if not _sname:
            _parts = [
                str(r.get("supplier_brand")   or "").strip(),
                str(r.get("supplier_index")   or "").strip(),
                str(r.get("supplier_coating") or "").strip(),
                str(r.get("supplier_treatment") or "").strip(),
            ]
            _sname = " ".join(p for p in _parts if p)
        return {
            "supplier_product_name": _sname,
            "supplier_brand":        str(r.get("supplier_brand")   or ""),
            "supplier_index":        str(r.get("supplier_index")   or ""),
            "supplier_coating":      str(r.get("supplier_coating") or ""),
            "supplier_treatment":    str(r.get("supplier_treatment") or ""),
            "mapped":                True,
        }
    return {"supplier_product_name": "", "supplier_brand": "",
            "supplier_index": "", "supplier_coating": "",
            "supplier_treatment": "", "mapped": False}


def _get_supplier_catalogue(supplier_id: str) -> List[dict]:
    """
    Return all previously saved supplier product names for this supplier.
    Used as quick-pick dropdown so staff don't retype common names.
    """
    if not supplier_id:
        return []
    rows = _q("""
        SELECT DISTINCT
            COALESCE(psm.supplier_product_name, '') AS name,
            COALESCE(psm.supplier_brand,         '') AS brand,
            COALESCE(psm.supplier_index,         '') AS idx,
            COALESCE(psm.supplier_coating,       '') AS coating,
            COALESCE(psm.supplier_treatment,     '') AS treatment
        FROM product_supplier_map psm
        WHERE psm.supplier_id = %(sid)s::uuid
          AND psm.is_active   = TRUE
          AND psm.supplier_product_name IS NOT NULL
          AND psm.supplier_product_name != ''
        ORDER BY psm.supplier_product_name
    """, {"sid": supplier_id})
    return rows or []


def _supplier_product_selector(
    supplier_id: str,
    existing_map: dict,
    key_prefix: str,
) -> dict:
    """
    Product-picker style supplier catalogue selector.

    Uses existing WIN54 tables only:
      products → supplier brand/product list
      ophthalmic_lens_specs → index/coating/treatment variants

    The selected values are saved into product_supplier_map as supplier-facing
    text, so supplier WhatsApp/PO uses their catalogue wording while our order
    still remains linked to our product_id.
    """
    old_name  = str(existing_map.get("supplier_product_name") or "").strip()
    old_brand = str(existing_map.get("supplier_brand") or "").strip()
    old_idx   = str(existing_map.get("supplier_index") or "").strip()
    old_coat  = str(existing_map.get("supplier_coating") or "").strip()
    old_treat = str(existing_map.get("supplier_treatment") or "").strip()

    catalogue = _get_supplier_catalogue(supplier_id)
    if old_name and not any(str(c.get("name") or "") == old_name for c in catalogue):
        catalogue = [{
            "name": old_name,
            "brand": old_brand,
            "idx": old_idx,
            "coating": old_coat,
            "treatment": old_treat,
        }] + catalogue

    if catalogue:
        names = [str(c.get("name") or "").strip() for c in catalogue if str(c.get("name") or "").strip()]
        names = list(dict.fromkeys(names))
        default_idx = names.index(old_name) if old_name in names else 0
        sel_name = st.selectbox(
            "Supplier Product",
            names,
            index=default_idx,
            key=f"{key_prefix}_sup_catalog_pick",
            help="Only products already tagged to this selected supplier are shown.",
        )
        row = next((c for c in catalogue if str(c.get("name") or "").strip() == sel_name), {})
        sel_brand = str(row.get("brand") or old_brand or "").strip()
        sel_idx = str(row.get("idx") or old_idx or "").strip()
        sel_coat = str(row.get("coating") or old_coat or "").strip()
        sel_treat = str(row.get("treatment") or old_treat or "").strip()
    else:
        st.info("No supplier catalogue exists for this supplier yet. Add the supplier-facing name once; future selections will be restricted to this supplier.")
        sel_brand = st.text_input("Supplier Brand", value=old_brand, key=f"{key_prefix}_sup_brand_manual")
        sel_name = st.text_input("Supplier Product", value=old_name, key=f"{key_prefix}_sup_product_manual")
        sel_idx = st.text_input("Index", value=old_idx, key=f"{key_prefix}_sup_idx_manual")
        sel_coat = st.text_input("Coating", value=old_coat, key=f"{key_prefix}_sup_coat_manual")
        sel_treat = st.text_input("Treatment", value=old_treat, key=f"{key_prefix}_sup_treat_manual")

    st.caption(
        f"Supplier will see: **{sel_brand} {sel_name}**"
        + (f" · {sel_idx}" if sel_idx else "")
        + (f" · {sel_coat}" if sel_coat else "")
        + (f" · {sel_treat}" if sel_treat and sel_treat != "Clear" else "")
    )
    return {
        "ok": True,
        "brand": sel_brand,
        "product_name": sel_name,
        "index": sel_idx,
        "coating": sel_coat,
        "treatment": sel_treat,
    }


# ── Inline per-line mapping form (used inside pipeline) ───────────────────────

def render_line_mapping_form(
    line: dict,
    supplier_id: str,
    supplier_name: str = "",
    key_prefix: str = "",
) -> bool:
    """
    Compact mapping form for a SINGLE order line (one eye).
    Pre-fills our product — no selection needed.
    Supplier products shown as dropdown from their catalogue.

    Returns True if a mapping was saved.
    """
    product_id   = str(line.get("product_id") or "")
    our_pname    = str(line.get("product_name") or "")
    our_brand    = str(line.get("brand") or "")
    eye          = str(line.get("eye_side") or "").upper()
    eye_label    = {"R": "👁 Right Eye", "L": "👁 Left Eye"}.get(eye, eye)

    if not product_id or not supplier_id:
        st.warning("Product or supplier not set — cannot map.")
        return False

    # ── Our product info (read-only) ──────────────────────────────────────
    import json as _jl
    _lp = line.get("lens_params") or {}
    if isinstance(_lp, str):
        try: _lp = _jl.loads(_lp)
        except: _lp = {}
    _our_idx  = str(_lp.get("lens_index") or _lp.get("index") or line.get("index_value") or "")
    _our_coat = str(_lp.get("coating") or _lp.get("coating_type") or line.get("coating") or "")

    st.markdown(
        f"<div style='background:#1e293b;border-left:3px solid #6366f1;"
        f"border-radius:0 6px 6px 0;padding:6px 12px;margin:4px 0 8px 0'>"
        f"<span style='color:#a5b4fc;font-size:0.7rem;font-weight:700'>{eye_label}</span>"
        f"<span style='color:#e2e8f0;font-weight:700;margin-left:8px'>"
        f"{our_brand} {our_pname}</span>"
        + (f"<span style='color:#64748b;font-size:0.75rem'> · Idx {_our_idx}</span>" if _our_idx else "")
        + (f"<span style='color:#64748b;font-size:0.75rem'> · {_our_coat}</span>" if _our_coat else "")
        + f"</div>",
        unsafe_allow_html=True,
    )

    # ── Existing mapping (if any) ─────────────────────────────────────────
    existing_map = get_supplier_product_name(product_id, supplier_id)
    _has_mapping = existing_map.get("mapped", False)

    _kp = key_prefix or f"slm_{product_id[:8]}_{eye}"

    st.markdown("**Supplier catalogue match**")
    st.caption("Same style as punching: Brand → Product → Index → Coating → Treatment.")
    _picked = _supplier_product_selector(supplier_id, existing_map, _kp)
    _final_name  = str(_picked.get("product_name") or "").strip()
    _final_brand = str(_picked.get("brand") or "").strip()
    _final_idx   = str(_picked.get("index") or "").strip()
    _final_coat  = str(_picked.get("coating") or "").strip()
    _final_treat = str(_picked.get("treatment") or "").strip()

    # ── Save ──────────────────────────────────────────────────────────────
    _btn_lbl = "💾 Update Mapping" if _has_mapping else "💾 Save Mapping"
    if st.button(_btn_lbl, key=f"{_kp}_save", type="primary",
                 use_container_width=True,
                 disabled=not _final_name):
        _ok = _upsert_supplier_mapping(
            product_id,
            supplier_id,
            _final_name,
            _final_brand,
            _final_idx,
            _final_coat,
            _final_treat,
        )
        if _ok:
            st.success(f"✅ Mapped: {our_pname} → {_final_name}")
            return True
    return False


# ── Admin UI (full table view for Shop Master / settings page) ────────────────

def render_supplier_product_map_admin(
    supplier_id: Optional[str] = None,
    product_id: Optional[str] = None,
    lines: Optional[List[dict]] = None,
) -> None:
    """
    Mapping UI.

    When called from pipeline with context:
      - lines:       list of order line dicts (R + L) — shows one form per line
      - supplier_id: pre-selected, no dropdown needed
      - product_id:  used only if lines is None (single product mode)

    When called from admin/settings (no lines):
      - Shows full filterable table + add form
    """
    st.markdown(
        "<div style='background:#0f172a;border:1px solid #1e3a5f;"
        "border-left:4px solid #6366f1;border-radius:8px;"
        "padding:8px 14px;margin-bottom:10px'>"
        "<span style='color:#a5b4fc;font-size:0.9rem;font-weight:800'>"
        "🔗 Map Products to Supplier Catalogue</span>"
        "<span style='color:#475569;font-size:0.75rem;display:block;margin-top:2px'>"
        "WhatsApp and procurement will use the supplier's product names below.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    sup_name = ""
    if supplier_id:
        _sn = _q("SELECT party_name FROM parties WHERE id=%(sid)s::uuid LIMIT 1",
                 {"sid": supplier_id})
        sup_name = _sn[0]["party_name"] if _sn else ""

    # ── MODE 1: Pipeline context — per-line forms ─────────────────────────
    if lines:
        _saved_any = False
        _eye_lines = [
            ln for ln in lines
            if str(ln.get("eye_side") or "").upper() in ("R", "L")
        ]
        _r_line = next((ln for ln in _eye_lines if str(ln.get("eye_side") or "").upper() == "R"), None)
        _l_line = next((ln for ln in _eye_lines if str(ln.get("eye_side") or "").upper() == "L"), None)
        if _r_line and _l_line:
            _r_sid = str(_r_line.get("supplier_id") or supplier_id or "")
            _l_sid = str(_l_line.get("supplier_id") or supplier_id or "")
            _r_pid = str(_r_line.get("product_id") or "")
            _l_pid = str(_l_line.get("product_id") or "")
            _r_map = get_supplier_product_name(_r_pid, _r_sid) if _r_pid and _r_sid else {}
            if st.button(
                "➡️ Save L same as Right mapping",
                key=f"spm_copy_r_to_l_{(_l_line.get('line_id') or _l_pid or 'L')}",
                use_container_width=True,
                disabled=not bool(_r_map.get("supplier_product_name") and _l_pid and _l_sid),
                help="Copies the saved R-eye supplier product/index/coating/treatment to L. You can still change L separately later.",
            ):
                if _upsert_supplier_mapping(
                    _l_pid,
                    _l_sid,
                    _r_map.get("supplier_product_name", ""),
                    _r_map.get("supplier_brand", ""),
                    _r_map.get("supplier_index", ""),
                    _r_map.get("supplier_coating", ""),
                    _r_map.get("supplier_treatment", ""),
                ):
                    st.success("✅ Left mapping copied from Right.")
                    st.rerun()

        for _ln in lines:
            _eye = str(_ln.get("eye_side") or "").upper()
            if _eye not in ("R", "L"):
                continue
            _sid = str(_ln.get("supplier_id") or supplier_id or "")
            _sname = str(_ln.get("supplier_name") or sup_name or "")
            _kp = f"lmf_{str(_ln.get('line_id') or _ln.get('id') or _eye)[:8]}"
            saved = render_line_mapping_form(_ln, _sid, _sname, key_prefix=_kp)
            if saved:
                _saved_any = True
            st.markdown("---")
        if _saved_any:
            st.rerun()
        return

    # ── MODE 2: Single product mode (product_id + supplier_id given) ──────
    if product_id and supplier_id:
        fake_line = {
            "product_id": product_id,
            "supplier_id": supplier_id,
        }
        _prows = _q("""
            SELECT product_name, brand,
                   COALESCE(coating_type,'') AS coating,
                   COALESCE(index_value::text,'') AS idx
            FROM products WHERE id=%(pid)s::uuid LIMIT 1
        """, {"pid": product_id})
        if _prows:
            fake_line.update({
                "product_name": _prows[0]["product_name"],
                "brand":        _prows[0]["brand"],
                "eye_side":     "",
            })
        render_line_mapping_form(fake_line, supplier_id, sup_name,
                                  key_prefix=f"spm_{product_id[:8]}")
        return

    # ── MODE 3: Full admin table ───────────────────────────────────────────
    products  = _q("""
        SELECT id::text AS id, product_name, brand,
               COALESCE(coating_type,'') AS coating,
               COALESCE(index_value::text,'') AS idx
        FROM products
        WHERE COALESCE(is_active,TRUE)=TRUE
          AND UPPER(COALESCE(main_group,'')) IN
              ('OPHTHALMIC','LENS','SPECTACLE LENS','CONTACT LENS')
        ORDER BY brand, product_name
    """)
    suppliers = _q("""
        SELECT id::text AS id, party_name FROM parties
        WHERE UPPER(COALESCE(party_type,'')) IN ('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB')
          AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name
    """)
    if not products or not suppliers:
        st.info("No products or suppliers found.")
        return

    prod_by_id = {p["id"]: p for p in products}
    sup_by_id  = {s["id"]: s for s in suppliers}

    _fc1, _fc2 = st.columns(2)
    _sel_sid = supplier_id or _fc1.selectbox(
        "Supplier", [""] + [s["id"] for s in suppliers],
        format_func=lambda x: "— All —" if not x else sup_by_id.get(x,{}).get("party_name",x),
        key="spm_adm_sup")
    _sel_pid = product_id or _fc2.selectbox(
        "Product", [""] + [p["id"] for p in products],
        format_func=lambda x: "— All —" if not x else
            f"{prod_by_id[x]['brand']} {prod_by_id[x]['product_name']}" if x in prod_by_id else x,
        key="spm_adm_prod")

    _where = ["psm.is_active=TRUE"]
    _par: dict = {}
    if _sel_sid: _where.append("psm.supplier_id=%(sid)s::uuid"); _par["sid"] = _sel_sid
    if _sel_pid: _where.append("psm.product_id=%(pid)s::uuid");  _par["pid"] = _sel_pid

    existing = _q(f"""
        SELECT psm.id::text AS map_id, psm.product_id::text, psm.supplier_id::text,
               COALESCE(p.product_name,'') AS our_product,
               COALESCE(p.brand,'') AS our_brand,
               COALESCE(p.coating_type,'') AS our_coating,
               COALESCE(p.index_value::text,'') AS our_index,
               COALESCE(pt.party_name,'') AS supplier_name,
               COALESCE(psm.supplier_product_name,'') AS sup_pname,
               COALESCE(psm.supplier_brand,'') AS sup_brand,
               COALESCE(psm.supplier_index,'') AS sup_index,
               COALESCE(psm.supplier_coating,'') AS sup_coating,
               COALESCE(psm.supplier_treatment,'') AS sup_treatment,
               COALESCE(psm.notes,'') AS notes
        FROM product_supplier_map psm
        JOIN products p  ON p.id  = psm.product_id
        JOIN parties  pt ON pt.id = psm.supplier_id
        WHERE {' AND '.join(_where)}
        ORDER BY pt.party_name, p.brand, p.product_name
    """, _par)

    if existing:
        st.caption(f"**{len(existing)} mapping(s)**")
        for row in existing:
            _disp = row["sup_pname"] or " ".join(filter(None,[row["sup_brand"],row["sup_index"],row["sup_coating"],row.get("sup_treatment","")])) or "not set"
            with st.expander(f"🔗 {row['our_brand']} {row['our_product']} → {row['supplier_name']}: **{_disp}**", expanded=False):
                _e1, _e2 = st.columns(2)
                _e1.markdown(f"**Our product**\n\nBrand: {row['our_brand']}\nProduct: {row['our_product']}\nIndex: {row['our_index']}\nCoating: {row['our_coating']}")
                _mid = row["map_id"]
                _sid_row = row["supplier_id"]
                _cat = _get_supplier_catalogue(_sid_row)
                _cat_names = [c["name"] for c in _cat if c["name"] and c["name"] != row["sup_pname"]]
                if _cat_names:
                    _e2.caption("Previously used names for this supplier:")
                    _pick = _e2.selectbox("Quick-pick", ["—"] + _cat_names, key=f"spm_qp_{_mid}")
                    if _pick != "—":
                        _cat_r = next((c for c in _cat if c["name"]==_pick), {})
                        st.session_state[f"spm_spn_{_mid}"] = _pick
                        st.session_state[f"spm_sb_{_mid}"]  = _cat_r.get("brand","")
                        st.session_state[f"spm_si_{_mid}"]  = _cat_r.get("idx","")
                        st.session_state[f"spm_sc_{_mid}"]  = _cat_r.get("coating","")
                        st.session_state[f"spm_st_{_mid}"]  = _cat_r.get("treatment","")
                _spn  = _e2.text_input("Supplier Product Name", value=row["sup_pname"], key=f"spm_spn_{_mid}", placeholder="e.g. Alfa 1.50 HC/AR")
                _sb   = _e2.text_input("Supplier Brand",        value=row["sup_brand"], key=f"spm_sb_{_mid}")
                _si   = _e2.text_input("Supplier Index",        value=row["sup_index"], key=f"spm_si_{_mid}")
                _sc   = _e2.text_input("Supplier Coating",      value=row["sup_coating"], key=f"spm_sc_{_mid}")
                _st   = _e2.text_input("Supplier Treatment",    value=row.get("sup_treatment",""), key=f"spm_st_{_mid}")
                _sn   = st.text_input("Notes", value=row["notes"], key=f"spm_notes_{_mid}")
                _s1, _s2 = st.columns(2)
                if _s1.button("💾 Save", key=f"spm_save_{_mid}", type="primary", use_container_width=True):
                    if _w("UPDATE product_supplier_map SET supplier_product_name=%(spn)s,supplier_brand=%(sb)s,supplier_index=%(si)s,supplier_coating=%(sc)s,supplier_treatment=%(st)s,notes=%(n)s,updated_at=NOW() WHERE id=%(mid)s::uuid",
                          {"spn":_spn.strip(),"sb":_sb.strip(),"si":_si.strip(),"sc":_sc.strip(),"st":_st.strip(),"n":_sn.strip(),"mid":_mid}):
                        st.success("✅ Updated"); st.rerun()
                if _s2.button("🗑 Remove", key=f"spm_del_{_mid}", use_container_width=True):
                    _w("UPDATE product_supplier_map SET is_active=FALSE WHERE id=%(mid)s::uuid",{"mid":_mid}); st.rerun()
    else:
        st.info("No mappings yet for this filter.")

    st.markdown("---")
    with st.expander("➕ Add New Mapping", expanded=not existing):
        _n1, _n2 = st.columns(2)
        _np = _n1.selectbox("Our Product *", [""]+[p["id"] for p in products],
            format_func=lambda x: "— Select —" if not x else f"{prod_by_id[x]['brand']} {prod_by_id[x]['product_name']} (Idx {prod_by_id[x]['idx']} {prod_by_id[x]['coating']})" if x in prod_by_id else x,
            key="spm_new_pid")
        _ns = _n2.selectbox("Supplier / Lab *", [""]+[s["id"] for s in suppliers],
            format_func=lambda x: "— Select —" if not x else sup_by_id.get(x,{}).get("party_name",x),
            key="spm_new_sid",
            index=([""]+[s["id"] for s in suppliers]).index(_sel_sid) if _sel_sid and _sel_sid in [s["id"] for s in suppliers] else 0)

        # Supplier catalogue quick-pick
        _add_cat = _get_supplier_catalogue(_ns) if _ns else []
        _add_cat_names = [c["name"] for c in _add_cat if c["name"]]
        _add_spn = _add_brand = _add_idx = _add_coat = _add_treat = ""
        if _add_cat_names:
            _cat_pick = st.selectbox("Quick-pick from supplier catalogue",
                ["— Type new —"] + _add_cat_names, key="spm_add_qpick")
            if _cat_pick != "— Type new —":
                _cr = next((c for c in _add_cat if c["name"]==_cat_pick),{})
                _add_spn   = _cat_pick
                _add_brand = _cr.get("brand","")
                _add_idx   = _cr.get("idx","")
                _add_coat  = _cr.get("coating","")
                _add_treat = _cr.get("treatment","")
                st.caption(f"Using: **{_add_spn}** {_add_brand} {_add_idx} {_add_coat} {_add_treat}")

        if not _add_spn:
            _m1, _m2 = st.columns(2)
            _add_spn   = _m1.text_input("Supplier Product Name *", key="spm_add_spn",  placeholder="e.g. Alfa 1.50 HC/AR")
            _add_brand = _m2.text_input("Supplier Brand",          key="spm_add_brand",placeholder="e.g. Bonzer Lenses")
            _add_idx   = _m1.text_input("Supplier Index",          key="spm_add_idx",  placeholder="e.g. 1.50")
            _add_coat  = _m2.text_input("Supplier Coating",        key="spm_add_coat", placeholder="e.g. HC/AR")
            _add_treat = _m1.text_input("Supplier Treatment",      key="spm_add_treat", placeholder="e.g. Clear / Blue Block")
        _add_notes = st.text_input("Notes (optional)", key="spm_add_notes")

        if st.button("➕ Add Mapping", key="spm_add_btn", type="primary",
                     use_container_width=True, disabled=(not _np or not _ns or not _add_spn)):
            if _upsert_supplier_mapping(
                _np,
                _ns,
                _add_spn,
                _add_brand,
                _add_idx,
                _add_coat,
                _add_treat,
                notes=_add_notes,
            ):
                st.success("✅ Mapping added"); st.rerun()
