"""
modules/procurement/invoice_match_ui.py
========================================
3-Phase AI Supplier Invoice Matching UI.

Phase 1 — Upload + Review        (zero DB writes)
Phase 2 — Map + Train             (writes only to product_supplier_map)
Phase 3 — Controlled Posting      (atomic transaction, duplicate-blocked)

Confidence gating:
  >= 0.95  → ✅ Auto-accept
  0.80-0.95 → ⚠️ Amber — user confirms
  < 0.80   → ❌ Mandatory manual selection
"""
from __future__ import annotations
import datetime as _dt, json, re
from typing import Any, Dict, List, Optional
import streamlit as st
from modules.procurement.supplier_invoice_rules import (
    ai_match_product, ensure_table as _ensure_rules_table,
    extract_header_fields, get_rules, list_all_rules,
    normalise_product_text, parse_power_alcon_toric,
    parse_power_merged_rl, resolve_supplier, save_rules,
)

def _save_invoice_match_upload(uploaded, ref_no: str = "") -> tuple[str, str]:
    """Store original supplier invoice plus compressed image preview if possible."""
    import pathlib
    original_dir = pathlib.Path("uploads/purchase_invoices")
    preview_dir = pathlib.Path("uploads/purchase_invoice_previews")
    original_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(ref_no or "draft")).strip("-") or "draft"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(uploaded.name or "invoice")).strip("-") or "invoice"
    original_path = original_dir / f"{_dt.date.today().isoformat()}_{safe_ref}_{safe_name}"
    data = uploaded.getvalue()
    with open(original_path, "wb") as fh:
        fh.write(data)

    preview_path = ""
    if original_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        try:
            from PIL import Image
            img = Image.open(original_path)
            img.thumbnail((1600, 1600))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            preview = preview_dir / f"{original_path.stem}_preview.jpg"
            img.save(preview, format="JPEG", quality=82, optimize=True)
            preview_path = str(preview)
        except Exception:
            preview_path = ""
    return str(original_path), preview_path


def _actor_name() -> str:
    try:
        user = st.session_state.get("user") or {}
        return str(user.get("name") or user.get("username") or st.session_state.get("user_name") or "System")
    except Exception:
        return "System"

# ── DB helpers ────────────────────────────────────────────────────────────────
def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or {}) or []
    except Exception as e:
        st.error(f"DB: {e}"); return []

def _w(sql, params=None):
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params or {}); return True
    except Exception as e:
        st.error(f"Write: {e}"); return False

# ── Ophthalmic spec helpers (same pattern as ui_product_selector) ─────────────
# NOTE: previously @st.cache_data(ttl=120). Removed because:
#   (a) a stale empty result from an earlier failed call would poison the
#       cache for 120s — exactly the "Index/Coating dropdown empty even
#       though specs exist in DB" symptom investigated this session.
#   (b) the underlying query is small (a few rows per product) and runs
#       only when the user opens a row — caching saved nothing meaningful.
# Also added explicit ::uuid cast on the WHERE clause so a stray
# whitespace/encoding artefact in product_id cannot silently match zero rows
# via psycopg's implicit string→uuid coercion.
def _oph_indices(product_id: str) -> list:
    """Available index values for a product from ophthalmic_lens_specs."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT index_value::text AS idx
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s::uuid AND COALESCE(is_active,TRUE)=TRUE
            ORDER BY index_value
        """, {"pid": str(product_id).strip()}) or []
        return [r["idx"] for r in rows if r.get("idx")]
    except Exception as _ie:
        import logging
        logging.getLogger(__name__).warning(
            "_oph_indices failed for product_id=%r: %s", product_id, _ie
        )
        return []


def _oph_coatings(product_id: str, index_value: str) -> list:
    """Available coatings for product + index from ophthalmic_lens_specs."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT DISTINCT coating
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s::uuid AND index_value=%(idx)s::numeric
              AND COALESCE(is_active,TRUE)=TRUE
            ORDER BY coating
        """, {"pid": str(product_id).strip(), "idx": index_value}) or []
        return [r["coating"] for r in rows if r.get("coating")]
    except Exception as _ce:
        import logging
        logging.getLogger(__name__).warning(
            "_oph_coatings failed for product_id=%r idx=%r: %s",
            product_id, index_value, _ce,
        )
        return []


@st.cache_data(ttl=120, show_spinner=False)
def _oph_purchase_rate(product_id: str, index_value: str, coating: str) -> float:
    """Purchase rate for product + index + coating from ophthalmic_lens_specs."""
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT COALESCE(purchase_rate, 0) AS pr
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s AND index_value=%(idx)s::numeric
              AND coating=%(coat)s AND COALESCE(is_active,TRUE)=TRUE
            ORDER BY updated_at DESC NULLS LAST LIMIT 1
        """, {"pid": product_id, "idx": index_value, "coat": coating}) or []
        return float(rows[0].get("pr") or 0) if rows else 0.0
    except Exception:
        return 0.0


# ── Cached loaders ────────────────────────────────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def _suppliers():
    try:
        from modules.sql_adapter import run_query
        return run_query(
            "SELECT id::text, party_name FROM parties "
            "WHERE UPPER(COALESCE(party_type,'')) IN "
            "('SUPPLIER','VENDOR','LAB','EXTERNAL_LAB','CONTACT_LENS_SUPPLIER') "
            "AND COALESCE(is_active,TRUE)=TRUE ORDER BY party_name") or []
    except Exception: return []

@st.cache_data(ttl=120, show_spinner=False)
def _products():
    try:
        from modules.sql_adapter import run_query
        return run_query("""
            SELECT p.id::text AS id, p.product_name, p.brand,
                   COALESCE(p.main_group,'') AS main_group,
                   COALESCE(p.category,'')   AS category,
                   p.index_value,
                   COALESCE(p.coating_type, '') AS coating_type,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'index_value', s.index_value,
                           'coating', s.coating,
                           'treatment', s.treatment
                       ))
                       FROM ophthalmic_lens_specs s
                       WHERE s.product_id=p.id
                         AND COALESCE(s.is_active, TRUE)=TRUE
                   ), '[]'::jsonb) AS oph_specs,
                   COALESCE(p.box_size,1) AS box_size,
                   COALESCE(p.gst_percent,12) AS gst_percent,
                   COALESCE((
                       SELECT COALESCE(NULLIF(s.purchase_price,0), s.purchase_rate, 0)
                       FROM inventory_stock s WHERE s.product_id=p.id
                       AND COALESCE(s.is_active,TRUE)=TRUE
                       ORDER BY s.updated_at DESC NULLS LAST LIMIT 1
                   ),0) AS purchase_rate
            FROM products p WHERE COALESCE(p.is_active,TRUE)=TRUE
            ORDER BY p.brand, p.product_name LIMIT 5000""") or []
    except Exception: return []

# ── Session helpers ───────────────────────────────────────────────────────────
def _g(k, d=None):  return st.session_state.get(k, d)
def _p(k, v):        st.session_state[k] = v
def _reset():
    for k in [x for x in st.session_state if x.startswith("imui_")]:
        del st.session_state[k]


def _invoice_total_parts(rows: list, totals: dict | None = None) -> dict:
    totals = totals or {}
    taxable = sum(
        float(r.get("taxable_value") or (float(r.get("qty") or 0) * float(r.get("unit_price") or 0)))
        for r in rows
    )
    gst = sum(float(r.get("gst_amount") or 0) for r in rows)
    discount = sum(float(r.get("discount") or 0) for r in rows)
    courier = float(totals.get("courier_charges") or totals.get("courier") or 0)
    invoice_gst = float(totals.get("total_gst") or 0)
    invoice_total = float(totals.get("total_amount") or 0)
    if invoice_gst > 0:
        gst = invoice_gst
    grand = invoice_total if invoice_total > 0 else taxable + gst + courier
    return {
        "taxable": round(taxable, 2),
        "gst": round(gst, 2),
        "discount": round(discount, 2),
        "courier": round(courier, 2),
        "grand": round(grand, 2),
    }


def _render_oph_spec_selector(rk: str, product_id: str, inv_desc: str = "") -> tuple[str, str, str]:
    """Render Index + Coating selectors for ophthalmic/spec products."""
    if inv_desc:
        st.session_state[f"{rk}_inv_desc"] = inv_desc
    _sel_index = ""
    _sel_coating = ""
    _sel_treatment = ""
    _invoice_arc = ""
    _invoice_treatment = ""
    # Normalize: callers occasionally pass None / whitespace / non-string UUIDs.
    # Without this, downstream SQL %(pid)s::uuid silently matches no rows
    # → indices empty → user sees blank Spec section with no explanation.
    product_id = (str(product_id).strip() if product_id is not None else "")
    if not product_id:
        st.session_state[f"{rk}_spec_index"] = ""
        st.session_state[f"{rk}_spec_coating"] = ""
        st.session_state[f"{rk}_spec_treatment"] = ""
        # Quiet diagnostic so the user sees WHY the section is empty.
        # Without this the row shows nothing where Index/Coating/Treatment
        # were expected and looks broken.
        st.caption(
            "📋 Spec mapping unavailable until a product is selected above."
        )
        return _sel_index, _sel_coating, _sel_treatment
    _pid_key = f"{rk}_spec_product_id_v3"
    if st.session_state.get(_pid_key) != product_id:
        for _spec_key in (
            f"{rk}_spec_index",
            f"{rk}_spec_coating",
            f"{rk}_spec_treatment",
            f"{rk}_idx_v3",
            f"{rk}_coat_v3",
            f"{rk}_treat_v3",
            f"{rk}_idx_free_v3",
            f"{rk}_coat_free_v3",
            f"{rk}_treat_free_v3",
        ):
            st.session_state.pop(_spec_key, None)
        st.session_state[_pid_key] = product_id

    _bz2 = {}
    try:
        from modules.procurement.supplier_invoice_rules import parse_bonzer_description
        _bz2 = parse_bonzer_description(
            st.session_state.get(f"{rk}_inv_desc", "") or ""
        )
    except Exception:
        _bz2 = {}
    _invoice_arc = str(_bz2.get("treatment") or "").upper()
    _invoice_treatment = str(_bz2.get("coating") or "").upper()
    _treatment_alias = {
        "BLUEBLOCK": "Blue Block",
        "CLEAR": "Clear",
        "PHOTOCHROMIC": "Photochromic",
        "TINTED": "Tinted",
    }

    st.markdown(
        "<div style='color:#a5b4fc;font-size:0.74rem;font-weight:700;"
        "margin-top:4px'>Lens Spec Mapping</div>",
        unsafe_allow_html=True,
    )

    _idx_list = _oph_indices(product_id)
    if not _idx_list:
        # Resolve the product name for an unambiguous diagnostic — so you
        # can immediately tell if the WRONG product matched (vs a genuinely
        # spec-less product). Without this the user sees "no specs" and
        # cannot tell which of the two failure modes it is.
        _pname_for_caption = ""
        try:
            _pn_rows = _q(
                "SELECT product_name FROM products WHERE id=%(p)s::uuid LIMIT 1",
                {"p": product_id},
            ) or []
            if _pn_rows:
                _pname_for_caption = str(_pn_rows[0].get("product_name") or "")
        except Exception:
            pass
        st.caption(
            f"No DB spec rows for "
            + (f"**{_pname_for_caption}** " if _pname_for_caption else "")
            + f"(product_id `{product_id[:8]}…`). "
            "If this isn't the right product, change it above. "
            "If it is, add its specs in master to get dropdowns."
        )
        _f1, _f2, _f3 = st.columns(3)
        _sel_index = _f1.text_input(
            "Index",
            value=str(_bz2.get("index") or ""),
            key=f"{rk}_idx_free_v3",
            placeholder="1.56",
        )
        _sel_coating = _f2.text_input(
            "Coating",
            value=str(_bz2.get("treatment") or ""),
            key=f"{rk}_coat_free_v3",
            placeholder="Murk / Iridio / Magnetic...",
        )
        _sel_treatment = _f3.text_input(
            "Treatment",
            value=_treatment_alias.get(_invoice_treatment, str(_bz2.get("coating") or "")),
            key=f"{rk}_treat_free_v3",
            placeholder="Clear / Blue Block / PG...",
        )
        st.session_state[f"{rk}_spec_index"] = _sel_index
        st.session_state[f"{rk}_spec_coating"] = _sel_coating
        st.session_state[f"{rk}_spec_treatment"] = _sel_treatment
        return _sel_index, _sel_coating, _sel_treatment

    _ai_idx = ""
    _ai_idx = str(_bz2.get("index") or "")

    _idx_opts = [""] + _idx_list
    _idx_default = _idx_opts.index(_ai_idx) if _ai_idx in _idx_opts else 0
    _sp1, _sp2, _sp3 = st.columns(3)
    _sel_index = _sp1.selectbox(
        "Index", _idx_opts, index=_idx_default, key=f"{rk}_idx_v3",
        format_func=lambda x: f"Index {x}" if x else "— Select Index —")
    st.session_state[f"{rk}_spec_index"] = _sel_index or ""
    if not _sel_index:
        st.session_state[f"{rk}_spec_coating"] = ""
        st.session_state[f"{rk}_spec_treatment"] = ""
        return _sel_index, _sel_coating, _sel_treatment

    _coat_list = _oph_coatings(product_id, _sel_index)
    if _coat_list:
        _ai_coat = ""
        try:
            _raw_norm = str(_bz2.get("raw") or "").upper()
            for _c in _coat_list:
                _cu = _c.upper()
                if (
                    (_invoice_arc and (_invoice_arc[:4] in _cu or _cu[:4] in _invoice_arc))
                    or (_raw_norm and any(tok in _cu for tok in _raw_norm.split() if len(tok) > 4))
                ):
                    _ai_coat = _c
                    break
        except Exception:
            pass
        _coat_opts = [""] + _coat_list
        _coat_default = (
            _coat_opts.index(_ai_coat) if _ai_coat in _coat_opts else 0
        )
        _sel_coating = _sp2.selectbox(
            "Coating", _coat_opts, index=_coat_default, key=f"{rk}_coat_v3",
            format_func=lambda x: x if x else "— Select Coating —")
        st.session_state[f"{rk}_spec_coating"] = _sel_coating or ""
        if not _sel_coating:
            st.session_state[f"{rk}_spec_treatment"] = ""
            return _sel_index, _sel_coating, _sel_treatment
        _spec_rate = _oph_purchase_rate(product_id, _sel_index, _sel_coating)
        if _spec_rate > 0:
            st.caption(f"📋 Spec purchase rate: ₹{_spec_rate:,.2f}")
    else:
        _sel_coating = _sp2.text_input(
            "Coating",
            value=str(_bz2.get("treatment") or ""),
            key=f"{rk}_coat_free_v3",
            placeholder="No DB coating for this index",
        )
        st.session_state[f"{rk}_spec_coating"] = _sel_coating or ""
        st.caption("No DB coating rows found for the selected index. Add the coating in lens specs to get a dropdown.")
    _treat_list = []
    try:
        _treat_rows = _q("""
            SELECT DISTINCT COALESCE(treatment,'') AS treatment
            FROM ophthalmic_lens_specs
            WHERE product_id=%(pid)s::uuid
              AND index_value=%(idx)s::numeric
              AND (%(coat)s='' OR coating=%(coat)s)
              AND COALESCE(is_active, TRUE)=TRUE
            ORDER BY treatment
        """, {"pid": product_id, "idx": _sel_index, "coat": _sel_coating or ""}) or []
        _treat_list = [r["treatment"] for r in _treat_rows if r.get("treatment")]
    except Exception:
        _treat_list = []
    if _treat_list:
        _ai_treat = _treatment_alias.get(_invoice_treatment, "")
        _treat_opts = [""] + _treat_list
        _treat_default = _treat_opts.index(_ai_treat) if _ai_treat in _treat_opts else 0
        _sel_treatment = _sp3.selectbox(
            "Treatment", _treat_opts, index=_treat_default,
            key=f"{rk}_treat_v3",
            format_func=lambda x: x if x else "— Select Treatment —")
        st.session_state[f"{rk}_spec_treatment"] = _sel_treatment or ""
    else:
        _sel_treatment = _sp3.text_input(
            "Treatment",
            value=_treatment_alias.get(_invoice_treatment, str(_bz2.get("coating") or "")),
            key=f"{rk}_treat_free_v3",
            placeholder="No DB treatment for this spec",
        )
        st.session_state[f"{rk}_spec_treatment"] = _sel_treatment or ""

    return _sel_index, _sel_coating, _sel_treatment

# ── Confidence gating ─────────────────────────────────────────────────────────
def _conf_tier(conf: float):
    """Returns (tier, color, label, auto_accept)"""
    if conf >= 0.95: return "HIGH",   "#10b981", f"✅ Auto-match ({conf*100:.0f}%)", True
    if conf >= 0.80: return "MEDIUM", "#f59e0b", f"⚠️ Confirm ({conf*100:.0f}%)",    False
    return               "LOW",    "#ef4444", f"❌ Manual select ({conf*100:.0f}%)", False

# ── Phase bar ─────────────────────────────────────────────────────────────────
def _bar(phase: int):
    labels = ["1 · Upload & Review", "2 · Map & Train", "3 · Post"]
    cols = st.columns(3)
    for i, (col, lbl) in enumerate(zip(cols, labels), 1):
        done, active = i<phase, i==phase
        bg = "#10b981" if done else ("#6366f1" if active else "#1e293b")
        fc = "#fff" if (done or active) else "#475569"
        col.markdown(
            f"<div style='text-align:center;background:{bg};border-radius:6px;"
            f"padding:5px;font-size:0.75rem;font-weight:700;color:{fc}'>"
            f"{'✓ ' if done else ''}{lbl}</div>", unsafe_allow_html=True)
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Upload + Review  (NO DB WRITES)
# ═══════════════════════════════════════════════════════════════════════════════
def _phase1():
    st.markdown("#### 📤 Upload Supplier Invoice")
    st.info("📋 **Phase 1 is read-only.** Nothing is saved to the database until Phase 3.", icon="🔒")

    uploaded = st.file_uploader(
        "Invoice (PDF / PNG / JPG / WEBP)",
        type=["pdf","png","jpg","jpeg","webp"],
        key="imui_uploader", label_visibility="collapsed")
    if not uploaded: return

    disk_path, preview_path = _save_invoice_match_upload(uploaded, "invoice-match")

    with st.spinner("Parsing invoice…"):
        try:
            from modules.procurement.invoice_image_ocr import parse_invoice_file
            parsed = parse_invoice_file(disk_path)
        except Exception:
            try:
                from modules.procurement.supplier_invoice_parser import parse_supplier_invoice_pdf
                parsed = parse_supplier_invoice_pdf(disk_path)
            except Exception as exc:
                st.error(f"Parse failed: {exc}"); return

    raw_text = str(parsed.get("raw_text_preview") or "")
    header   = parsed.get("header") or {}
    parties  = _suppliers()
    matched, conf = resolve_supplier(header, parties)

    # ── Supplier ──────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**🏭 Supplier**")
    detected = str(header.get("supplier") or "")
    sup_ids  = [""] + [p["id"] for p in parties]
    sup_lbls = {"": "— Select supplier —"}
    sup_lbls.update({p["id"]: p["party_name"] for p in parties})
    default_idx = (sup_ids.index(matched["id"]) if matched and matched["id"] in sup_ids else 0)

    if matched and conf >= 0.75:
        st.success(f"✅ {matched['party_name']} ({conf*100:.0f}% confidence)")
    elif detected:
        st.warning(f"⚠️ Could not match '{detected}' — select manually.")

    sel_sup = st.selectbox("Supplier", sup_ids, index=default_idx,
                            format_func=lambda x: sup_lbls.get(x,x),
                            key="imui_p1_sup", label_visibility="collapsed")

    # New supplier quick-create
    with st.expander("➕ Supplier not in list?", expanded=False):
        new_name = st.text_input("New supplier name", key="imui_new_sup",
                                  placeholder="e.g. Zeiss India Pvt Ltd")
        if st.button("Create supplier", key="imui_create_sup") and new_name.strip():
            if _w("INSERT INTO parties (party_name,party_type,is_active,created_at) "
                   "VALUES (%(n)s,'SUPPLIER',TRUE,NOW())", {"n": new_name.strip()}):
                _suppliers.clear()
                st.success(f"Created '{new_name.strip()}'. Select from dropdown.")
                st.rerun()

    # ── Header ────────────────────────────────────────────────────────────────
    sup_name = sup_lbls.get(sel_sup, detected)
    rules    = get_rules(supplier_name=sup_name, supplier_id=sel_sup)
    extr     = extract_header_fields(raw_text, rules)

    st.markdown("**📋 Header**")
    h1, h2, h3 = st.columns(3)
    inv_no   = h1.text_input("Invoice / Challan No ✱",
                               value=extr.get("invoice_no",""), key="imui_p1_inv")
    doc_date = h2.text_input("Date ✱", value=extr.get("date",""),
                               key="imui_p1_date", placeholder="DD/MM/YYYY")
    doc_type = h3.selectbox("Type", ["INVOICE","CHALLAN","BOTH"], key="imui_p1_type")

    # ── Line preview ──────────────────────────────────────────────────────────
    items = _normalise_items(parsed, rules)
    st.markdown(f"**📦 {len(items)} Line(s) — Preview** *(read-only)*")
    for i, item in enumerate(items,1):
        pwr = _fmt_power(item.get("power") or {})
        st.markdown(
            f"<div style='background:#0a1628;border:1px solid #1e3a5f;"
            f"border-radius:5px;padding:5px 12px;margin:2px 0;font-size:0.77rem'>"
            f"<b style='color:#e2e8f0'>{i}. {item.get('description','')}</b>"
            f"<span style='color:#64748b'> · Qty {item.get('qty',0)}"
            f" · ₹{item.get('unit_price',0):.2f}"
            + (f" · Batch {item['batch_no']}" if item.get("batch_no") else "")
            + (f" · <span style='color:#a5b4fc'>{pwr}</span>" if pwr else "")
            + "</span></div>", unsafe_allow_html=True)

    if not items:
        st.warning("No line items detected. Check raw text below.")

    with st.expander("📄 Raw text", expanded=False):
        st.text(raw_text[:3000] or "No text.")

    # ── Gate ──────────────────────────────────────────────────────────────────
    missing = ([f"supplier"] if not sel_sup else []) + \
              ([f"invoice number"] if not inv_no.strip() else []) + \
              ([f"date"] if not doc_date.strip() else []) + \
              ([f"line items"] if not items else [])
    if missing:
        st.error(f"Required before proceeding: {', '.join(missing)}")
        return

    st.success("✅ Review complete — no data saved. Proceed to match products.")
    if st.button("Next → Map Products ›", type="primary", key="imui_p1_next"):
        try:
            from modules.core.date_guard import validate_not_future
            _ok_dt, _msg_dt = validate_not_future(doc_date.strip(), "Supplier document date")
        except Exception as _dg_e:
            _ok_dt, _msg_dt = False, f"Date validation failed: {_dg_e}"
        if not _ok_dt:
            st.error(_msg_dt)
            return
        _p("imui_disk_path",    disk_path)
        _p("imui_preview_path", preview_path)
        _p("imui_parsed",       parsed)
        _p("imui_raw_text",     raw_text)
        _p("imui_items",        items)
        _p("imui_sup_id",       sel_sup)
        _p("imui_sup_name",     sup_name)
        _p("imui_inv_no",       inv_no.strip())
        _p("imui_doc_date",     doc_date.strip())
        _p("imui_doc_type",     doc_type)
        _p("imui_rules",        rules)
        _p("imui_phase",        2)
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Map + Train  (writes only to product_supplier_map)
# ═══════════════════════════════════════════════════════════════════════════════
def _phase2():
    items    = _g("imui_items", [])
    sup_id   = _g("imui_sup_id", "")
    sup_name = _g("imui_sup_name", "")
    rules    = _g("imui_rules", {})
    prods    = _products()

    st.markdown("#### 🤖 AI Product Mapping")
    st.info(
        "💾 **Phase 2 saves only to the supplier product map** (alias training). "
        "No purchase records are created yet.", icon="📚")

    # Run AI matching once per session
    mk = f"imui_matches_{sup_id}_{len(items)}"
    if mk not in st.session_state:
        with st.spinner(f"Running 5-tier matcher on {len(items)} line(s)…"):
            _p(mk, [ai_match_product(
                it.get("description",""), prods, rules, sup_id
            ) for it in items])
    matches    = _g(mk, [{}]*len(items))
    prod_by_id = {str(p["id"]): p for p in prods}
    all_brands = sorted({p.get("brand","") for p in prods if p.get("brand")})

    confirmed, all_ok = [], True

    for idx, (item, match) in enumerate(zip(items, matches)):
        rk      = f"imui_r{idx}"
        desc    = item.get("description","") or f"Line {idx+1}"
        ai_pid  = match.get("product_id","")
        ai_conf = float(match.get("confidence") or 0)
        ai_meth = str(match.get("method",""))
        ai_pnm  = str(match.get("product_name",""))
        pwr     = item.get("power") or {}
        tier, clr, tier_lbl, auto_ok = _conf_tier(ai_conf)

        with st.container(border=True):
            # ── Invoice line ──────────────────────────────────────────────────
            st.markdown(
                f"<div style='font-size:0.73rem;color:#64748b'>📄 As per invoice:</div>"
                f"<div style='font-size:0.84rem;color:#e2e8f0;font-weight:700'>{desc}</div>"
                + (f"<div style='font-size:0.69rem;color:#a5b4fc'>💊 {_fmt_power(pwr)}</div>" if _fmt_power(pwr) else ""),
                unsafe_allow_html=True)

            # ── AI suggestion with confidence tier ────────────────────────────
            st.markdown(
                f"<div style='font-size:0.72rem;margin-top:4px'>"
                f"<span style='color:{clr};font-weight:700'>{tier_lbl}</span>"
                f"<span style='color:#475569'> · {ai_meth}"
                + (f" · {normalise_product_text(desc)}" if ai_meth in ("NORMALISE","DIFFLIB") else "")
                + f"</span></div>",
                unsafe_allow_html=True)

            # ── Accept / override based on confidence tier ────────────────────
            if tier == "HIGH":
                # Auto-accept: show as confirmed, offer override link
                approved = st.checkbox(
                    f"✓ {ai_pnm}",
                    value=True, key=f"{rk}_ok")
                final_pid = ai_pid
                if not approved:
                    all_ok = False
                    final_pid = _override_widget(
                        rk, ai_pid, all_brands, prods, prod_by_id, inv_desc=desc
                    )
                else:
                    _render_oph_spec_selector(rk, final_pid, desc)
                    # Power variant picker for any power-stocked product.
                    # Works for: CL (SPH+CYL+AX+BC), Bonzer stock (SPH+ADD),
                    # ophthalmic semi-finished — any product with power rows in inventory_stock.
                    _pid = final_pid
                    _epwr = item.get("power") or {}
                    try:
                        _apw = _q(
                            "SELECT id::text AS sid, "
                            "COALESCE(sph,0) AS sph, COALESCE(cyl,0) AS cyl, "
                            "COALESCE(axis,0) AS axis, COALESCE(add_power,0) AS add_p, "
                            "COALESCE(colour,'') AS colour, "
                            "COALESCE(quantity,0)-COALESCE(allocated_qty,0) AS qty "
                            "FROM inventory_stock WHERE product_id=%(p)s::uuid AND COALESCE(is_active,TRUE)=TRUE "
                            "AND (sph IS NOT NULL OR add_power IS NOT NULL) "
                            "ORDER BY sph, add_power, cyl, axis",
                            {"p": _pid}
                        )
                        if _apw:
                            def _apl(r):
                                sph = float(r.get("sph") or 0)
                                cyl = float(r.get("cyl") or 0)
                                ax  = int(r.get("axis") or 0)
                                add = float(r.get("add_p") or 0)
                                col = str(r.get("colour") or "")
                                qty = int(r.get("qty") or 0)
                                # Show all 4 power fields as labelled columns
                                parts = [f"SPH {sph:+.2f}"]
                                parts.append(f"CYL {cyl:+.2f}" if abs(cyl)>0.01 else "CYL —")
                                parts.append(f"AX {ax}" if ax>0 else "AX —")
                                parts.append(f"ADD {add:+.2f}" if abs(add)>0.01 else "")
                                if col: parts.append(col)
                                parts = [p for p in parts if p]
                                parts.append(f"[{qty}pcs]" if qty > 0 else "[New receipt]")
                                return "  |  ".join(parts)
                            _apw_ids  = [""] + [r["sid"] for r in _apw]
                            _apw_lbls = {"": "— Select power variant —"}
                            _apw_lbls.update({r["sid"]: _apl(r) for r in _apw})
                            # Auto-select matching variant from parsed invoice power
                            _auto_sel = ""
                            _esph  = float(_epwr.get("sph") or 0)
                            _ecyl  = float(_epwr.get("cyl") or 0)
                            _eaxis = int(_epwr.get("axis") or 0)
                            _eadd  = float(_epwr.get("add") or 0)
                            for _arow in _apw:
                                _rsph  = float(_arow.get("sph") or 0)
                                _rcyl  = float(_arow.get("cyl") or 0)
                                _raxis = int(_arow.get("axis") or 0)
                                _radd  = float(_arow.get("add_p") or 0)
                                if (abs(_rsph - _esph) < 0.02
                                        and abs(_rcyl - _ecyl) < 0.02
                                        and (_eaxis == 0 or _raxis == 0 or _eaxis == _raxis)
                                        and abs(_radd - _eadd) < 0.02):
                                    _auto_sel = _arow["sid"]; break
                            _apw_sel = st.selectbox(
                                "Power Variant",
                                _apw_ids,
                                index=_apw_ids.index(_auto_sel) if _auto_sel in _apw_ids else 0,
                                format_func=lambda x: _apw_lbls.get(x, x),
                                key=f"{rk}_auto_pw",
                            )
                            if _apw_sel:
                                st.session_state[f"{rk}_inv_stock_id"] = _apw_sel
                            else:
                                st.caption(
                                    "⚠️ Select the power variant to link this receipt to stock. "
                                    "Leave blank only if this is a new power not yet in inventory."
                                )
                    except Exception:
                        pass

            elif tier == "MEDIUM":
                # Amber: user must explicitly tick
                approved = st.checkbox(
                    f"⚠️ Confirm: **{ai_pnm or '—'}**",
                    value=False, key=f"{rk}_ok")
                final_pid = ai_pid
                if not approved:
                    all_ok = False
                final_pid = _override_widget(rk, ai_pid, all_brands, prods, prod_by_id, inv_desc=desc)

            else:
                # Low/no match: mandatory manual selection
                approved = False
                all_ok   = False
                st.markdown(
                    "<div style='color:#ef4444;font-size:0.72rem'>"
                    "❌ No confident match — select product manually:</div>",
                    unsafe_allow_html=True)
                final_pid = _override_widget(rk, ai_pid, all_brands, prods, prod_by_id, inv_desc=desc)
                if final_pid:
                    approved = True

            # ── Power-aware inventory_stock match ────────────────────────────
            # For contact lenses: find the exact stock row (product + power).
            # For custom Rx: no stock row exists — power comes from order.
            chosen  = prod_by_id.get(final_pid, {})
            pwr     = item.get("power") or {}
            if final_pid and pwr:
                try:
                    from modules.procurement.supplier_invoice_rules import (
                        match_to_inventory_stock, is_cl_product as _is_cl
                    )
                    _cl = _is_cl(final_pid, prods)
                    # Fallback: check product name for CL keywords
                    if not _cl:
                        _pn_lower = str(chosen.get("product_name","") or "").lower()
                        _cl = any(k in _pn_lower for k in (
                            "freshlook","air optix","acuvue","dailies","biofinity",
                            "aquasoft","celebration","lacelle","contact","cl ",
                            "natural look","polylite","1day","1 day","monthly",
                            "quarterly","yearly","toric","colorblend","colour",
                        ))
                    _stk_row = match_to_inventory_stock(final_pid, pwr, _cl)
                    if _stk_row:
                        _qty_avail = int(_stk_row.get("qty") or 0)
                        _batch_v   = _stk_row.get("batch_no") or None
                        if _batch_v and _qty_avail > 0:
                            st.markdown(
                                f"<div style='background:#052e16;border:1px solid #166534;"
                                f"border-radius:4px;padding:3px 8px;font-size:0.7rem;"
                                f"color:#86efac;margin:3px 0'>"
                                f"📦 In stock: Batch {_batch_v} · {_qty_avail} pcs"
                                f" · Exp {_stk_row.get('expiry_date','—')}"
                                f"</div>", unsafe_allow_html=True)
                        else:
                            st.markdown(
                                f"<div style='background:#0c1a2e;border:1px solid #1e3a5f;"
                                f"border-radius:4px;padding:3px 8px;font-size:0.7rem;"
                                f"color:#60a5fa;margin:3px 0'>"
                                f"🆕 Power in catalogue · no stock yet. Receipt adds stock."
                                f"</div>", unsafe_allow_html=True)
                    elif _cl:
                        st.markdown(
                            f"<div style='background:#1a0a00;border:1px solid #78350f;"
                            f"border-radius:4px;padding:3px 8px;font-size:0.7rem;"
                            f"color:#fcd34d;margin:3px 0'>"
                            f"🆕 New power variant — will be added to stock on receipt"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f"<div style='background:#0f172a;border:1px solid #1e3a5f;"
                            f"border-radius:4px;padding:3px 8px;font-size:0.7rem;"
                            f"color:#64748b;margin:3px 0'>"
                            f"📋 Custom Rx — power from order description (no pre-stocked variant)"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                except Exception:
                    pass

            # ── Qty / price / batch ───────────────────────────────────────────
            db_rate = float(chosen.get("purchase_rate") or 0)
            inv_qty = float(item.get("qty") or 1)
            inv_prc = float(item.get("unit_price") or 0)

            _gross_default = round(inv_qty * inv_prc, 2)
            _disc_default = float(item.get("discount") or 0)
            _gst_default = float(chosen.get("gst_percent") or 12)
            if item.get("gst_amount") and (item.get("taxable_value") or _gross_default):
                try:
                    _gst_default = round(
                        float(item.get("gst_amount") or 0)
                        * 100
                        / max(float(item.get("taxable_value") or _gross_default), 0.01),
                        2,
                    )
                except Exception:
                    pass

            pc1,pc2,pc3,pc4,pc5,pc6 = st.columns([0.8,0.9,0.9,0.8,1.2,1.2])
            qty    = pc1.number_input("Qty",   value=inv_qty, min_value=0.0,
                                       step=1.0, key=f"{rk}_qty",
                                       label_visibility="collapsed")
            rate   = pc2.number_input("₹/unit",
                                       value=inv_prc if inv_prc else db_rate,
                                       min_value=0.0, step=0.5, format="%.2f",
                                       key=f"{rk}_rate",
                                       label_visibility="collapsed")
            discount = pc3.number_input(
                "Discount",
                value=_disc_default,
                min_value=0.0,
                step=0.5,
                format="%.2f",
                key=f"{rk}_disc",
                label_visibility="collapsed",
            )
            gst_pct = pc4.number_input(
                "GST %",
                value=_gst_default,
                min_value=0.0,
                step=0.5,
                format="%.2f",
                key=f"{rk}_gst",
                label_visibility="collapsed",
            )
            batch  = pc5.text_input("Batch", value=item.get("batch_no",""),
                                     key=f"{rk}_batch", placeholder="Batch No",
                                     label_visibility="collapsed")
            expiry = pc6.text_input("Expiry", value=item.get("expiry_date",""),
                                     key=f"{rk}_expiry", placeholder="DD/MM/YYYY",
                                     label_visibility="collapsed")
            _line_taxable = max(round(float(qty) * float(rate) - float(discount), 2), 0.0)
            _line_gst_amt = round(_line_taxable * float(gst_pct) / 100, 2)
            st.caption(
                f"Taxable ₹{_line_taxable:,.2f} · GST ₹{_line_gst_amt:,.2f} · "
                f"Line total ₹{_line_taxable + _line_gst_amt:,.2f}"
            )

            if db_rate and rate and abs(rate-db_rate)/max(db_rate,1)>0.05:
                st.caption(f"⚠️ Invoice ₹{rate:.2f} vs DB ₹{db_rate:.2f} "
                           f"(Δ{(rate-db_rate)/db_rate*100:+.1f}%)")

            confirmed.append({
                "product_id":    final_pid,
                "product_name":  chosen.get("product_name","") or ai_pnm,
                "supplier_item": desc,
                "qty": qty, "unit_price": rate,
                "discount": discount,
                "gst_percent": gst_pct,
                "gst_amount": _line_gst_amt,
                "taxable_value": _line_taxable,
                "batch_no": batch.strip(), "expiry_date": expiry.strip(),
                "power_json": pwr,
                "sph":  pwr.get("sph"),
                "cyl":  pwr.get("cyl"),
                "axis": pwr.get("axis"),
                "add":  pwr.get("add"),
                "bc":   pwr.get("bc"),
                "dia":  pwr.get("dia"),
                # Index + Coating from ophthalmic_lens_specs selection
                "lens_index":  st.session_state.get(f"{rk}_spec_index",""),
                "coating":     st.session_state.get(f"{rk}_spec_coating",""),
                "treatment":   st.session_state.get(f"{rk}_spec_treatment",""),
                "ai_confidence": ai_conf, "ai_method": ai_meth,
                "approved": approved,
            })

    _p("imui_confirmed_rows", confirmed)
    st.markdown("---")
    ready = [r for r in confirmed if r.get("product_id") and r.get("approved")]
    totals = (_g("imui_parsed", {}) or {}).get("totals") or {}
    _parts = _invoice_total_parts(ready, totals)
    pct_ok = len(ready)==len(confirmed)
    st.markdown(
        f"<div style='text-align:right;font-size:0.88rem;font-weight:700;"
        f"color:{'#10b981' if pct_ok else '#f59e0b'}'>"
        f"{len(ready)}/{len(confirmed)} approved · "
        f"Taxable ₹{_parts['taxable']:,.2f}"
        + (f" · Courier ₹{_parts['courier']:,.2f}" if _parts["courier"] else "")
        + f" · GST ₹{_parts['gst']:,.2f} · Total ₹{_parts['grand']:,.2f}</div>",
        unsafe_allow_html=True)
    if not pct_ok:
        st.warning(f"⚠️ {len(confirmed)-len(ready)} line(s) need manual product selection. "
                    "All lines must be approved before posting.")

    cb1, cb2 = st.columns([1,3])
    if cb1.button("← Back", key="imui_p2_back"):
        _p("imui_phase",1); st.rerun()
    if cb2.button("💾 Save Mappings & Continue →", type="primary",
                   key="imui_p2_save", disabled=not ready):
        n = _save_maps(ready, sup_id, sup_name)
        _write_audit_log(ready, _g("imui_inv_no",""), sup_name)
        st.success(f"✅ {n} mapping(s) saved. Audit logged. Proceed to post.")
        _p("imui_confirmed_rows", confirmed)
        _p("imui_phase",3); st.rerun()


def _override_widget(rk, ai_pid, all_brands, prods, prod_by_id, inv_desc: str = "") -> str:
    """
    3-level selector: Brand → Product → Index → Coating → Power variant.
    inv_desc: raw invoice description for AI pre-fill of index/coating.
    """
    # Store description for AI pre-fill of index/coating
    if inv_desc:
        st.session_state[f"{rk}_inv_desc"] = inv_desc
    oc1, oc2 = st.columns([2, 4])
    gb = prod_by_id.get(ai_pid, {}).get("brand", "") if ai_pid else ""
    sel_brand = oc1.selectbox("Brand", ["All"] + all_brands,
        index=(["All"] + all_brands).index(gb) if gb in all_brands else 0,
        key=f"{rk}_brand", label_visibility="collapsed")

    fp = [p for p in prods if sel_brand == "All" or p.get("brand", "") == sel_brand]
    fp_ids  = [""] + [str(p["id"]) for p in fp]
    fp_lbls = {"": "— Select product —"}
    fp_lbls.update({
        str(p["id"]): f"{p.get('brand','')} · {p.get('product_name','')}"
        for p in fp
    })
    dpid = ai_pid if ai_pid in fp_ids else ""
    sel_pid = oc2.selectbox("Product", fp_ids,
        index=fp_ids.index(dpid) if dpid in fp_ids else 0,
        format_func=lambda x: fp_lbls.get(x, x),
        key=f"{rk}_ovr", label_visibility="collapsed")

    _render_oph_spec_selector(rk, sel_pid, inv_desc)

    # ── Level 3: power variant picker (CL, Bonzer stock, any power-stocked product) ─
    # Query inventory_stock for power rows. Adapts label to available fields:
    # Air Optix toric: S-3.00 · C-1.25 · Ax180 · BC8.70 · [6pcs]
    # Bonzer V2 stock: S+1.00 · Add+2.00 · [1pcs]
    # FreshLook:       S-0.00 · BC8.60 · Hazel · [6pcs]
    if sel_pid:
        try:
            pw_rows = _q(
                "SELECT id::text AS sid, "
                "COALESCE(sph,0) AS sph, COALESCE(cyl,0) AS cyl, "
                "COALESCE(axis,0) AS axis, COALESCE(add_power,0) AS add_p, "
                "COALESCE(colour,'') AS colour, "
                "COALESCE(quantity,0)-COALESCE(allocated_qty,0) AS qty "
                "FROM inventory_stock WHERE product_id=%(p)s::uuid AND COALESCE(is_active,TRUE)=TRUE "
                "AND (sph IS NOT NULL OR add_power IS NOT NULL) "
                "ORDER BY sph, add_power, cyl, axis",
                {"p": sel_pid}
            )
            if pw_rows:
                def _pw_lbl(r):
                    sph = float(r.get("sph") or 0)
                    cyl = float(r.get("cyl") or 0)
                    ax  = int(r.get("axis") or 0)
                    add = float(r.get("add_p") or 0)
                    col = str(r.get("colour") or "")
                    qty = int(r.get("qty") or 0)
                    parts = [f"SPH {sph:+.2f}"]
                    parts.append(f"CYL {cyl:+.2f}" if abs(cyl)>0.01 else "CYL —")
                    parts.append(f"AX {ax}" if ax>0 else "AX —")
                    parts.append(f"ADD {add:+.2f}" if abs(add)>0.01 else "")
                    if col: parts.append(col)
                    parts = [p for p in parts if p]
                    parts.append(f"[{qty}pcs]" if qty > 0 else "[New receipt]")
                    return "  |  ".join(parts)
                pw_ids  = [""] + [r["sid"] for r in pw_rows]
                pw_lbls = {"": "— Select power variant —"}
                pw_lbls.update({r["sid"]: _pw_lbl(r) for r in pw_rows})
                sel_pw = st.selectbox(
                    "Power / Variant",
                    pw_ids,
                    format_func=lambda x: pw_lbls.get(x, x),
                    key=f"{rk}_pw_var",
                )
                if sel_pw:
                    st.session_state[f"{rk}_inv_stock_id"] = sel_pw
                else:
                    st.caption(
                        "⚠️ Select power variant, or leave blank for a new power not yet in stock."
                    )
            else:
                st.caption("ℹ️ No power variants in stock — new power/batch will be added on receipt.")
        except Exception as _pw_e:
            st.caption(f"Power lookup: {_pw_e}")

    return sel_pid


def _save_maps(rows, supplier_id, supplier_name) -> int:
    """Save confirmed mappings to product_supplier_map — training step."""
    count = 0
    for r in rows:
        pid, item = r.get("product_id",""), r.get("supplier_item","")
        if not (pid and item and supplier_id): continue
        try:
            from modules.procurement.supplier_product_map_ui import upsert_supplier_map
            ok = upsert_supplier_map(product_id=pid, supplier_id=supplier_id,
                                      supplier_product_name=item[:200],
                                      notes=f"invoice-match · {r.get('ai_method','')} "
                                            f"{r.get('ai_confidence',0)*100:.0f}%")
        except ImportError:
            import uuid as _u
            ok = _w("""
                INSERT INTO product_supplier_map
                    (id, product_id, supplier_id, rank,
                     supplier_product_name, notes, route_type,
                     is_active, created_at, updated_at)
                VALUES (%(id)s::uuid, %(pid)s::uuid, %(sid)s::uuid, 1,
                        %(spn)s, %(notes)s, 'VENDOR', TRUE, NOW(), NOW())
                ON CONFLICT (product_id, supplier_id) WHERE is_active=TRUE
                DO UPDATE SET
                    supplier_product_name = EXCLUDED.supplier_product_name,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
            """, {"id": str(_u.uuid4()), "pid": pid, "sid": supplier_id,
                  "spn": item[:200],
                  "notes": f"invoice-match · {r.get('ai_method','')} "
                           f"{r.get('ai_confidence',0)*100:.0f}%"})
        if ok: count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Controlled Posting  (atomic transaction, duplicate-blocked)
# ═══════════════════════════════════════════════════════════════════════════════
def _phase3():
    rows     = _g("imui_confirmed_rows", [])
    ready    = [r for r in rows if r.get("product_id") and r.get("approved")]
    sup_id   = _g("imui_sup_id","")
    sup_name = _g("imui_sup_name","")
    inv_no   = _g("imui_inv_no","")
    doc_date = _g("imui_doc_date","")
    doc_type = _g("imui_doc_type","INVOICE")
    path     = _g("imui_disk_path","")
    totals   = (_g("imui_parsed", {}) or {}).get("totals") or {}
    _parts = _invoice_total_parts(ready, totals)
    total = _parts["taxable"]
    courier_amt = _parts["courier"]
    invoice_gst = float(totals.get("total_gst") or 0)
    invoice_total = float(totals.get("total_amount") or 0)

    st.markdown("#### ✅ Controlled Posting")
    st.warning(
        "⚠️ **This is irreversible.** After posting, lines appear in Purchase Register. "
        "Void via Purchase Register if correction needed.", icon="🔒")

    # ── Duplicate check ───────────────────────────────────────────────────────
    dup = _q(
        "SELECT COUNT(*) AS n FROM purchase_acknowledgements "
        "WHERE LOWER(COALESCE(invoice_no,''))=LOWER(%(inv)s) "
        "AND (%(sid)s='' OR supplier_id=NULLIF(%(sid)s,'')::uuid)",
        {"inv": inv_no, "sid": sup_id or ""})
    dup_count = int((dup[0].get("n") or 0) if dup else 0)
    if dup_count > 0:
        st.error(
            f"🚫 **Duplicate blocked.** Invoice **{inv_no}** from **{sup_name}** "
            f"already has {dup_count} line(s) in the register.")
        st.caption("Void the existing entry in Purchase Register, or verify this is a different invoice.")
        if st.button("← Back", key="imui_p3_dup_back"):
            _p("imui_phase",2); st.rerun()
        return

    # ── Summary ───────────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:#052e16;border:1px solid #166534;border-radius:8px;"
        f"padding:12px 16px;margin-bottom:10px'>"
        f"<b style='color:#86efac'>Ready to Post</b><br>"
        f"<span style='color:#4ade80'>Supplier: {sup_name}</span><br>"
        f"<span style='color:#4ade80'>{inv_no} · {doc_date} · {doc_type}</span><br>"
        f"<span style='color:#4ade80'>{len(ready)} line(s) · Taxable ₹{total:,.2f}</span>"
        + (f"<br><span style='color:#4ade80'>Courier ₹{courier_amt:,.2f}</span>" if courier_amt else "")
        + (f"<br><span style='color:#4ade80'>Invoice GST ₹{invoice_gst:,.2f} · Total ₹{invoice_total:,.2f}</span>" if (invoice_gst or invoice_total) else "")
        + f"</div>", unsafe_allow_html=True)
    for r in ready:
        st.markdown(
            f"<div style='background:#080f1a;border:1px solid #1e293b;border-radius:4px;"
            f"padding:4px 10px;margin:2px 0;font-size:0.78rem'>"
            f"<b style='color:#e2e8f0'>{r['product_name']}</b> · "
            f"Qty {r['qty']} · ₹{r['unit_price']:.2f}"
            + (f" · Disc ₹{float(r.get('discount') or 0):.2f}" if float(r.get("discount") or 0) else "")
            + (f" · GST {float(r.get('gst_percent') or 0):.2f}%" if float(r.get("gst_percent") or 0) else "")
            + (f" · Batch {r['batch_no']}" if r.get("batch_no") else "")
            + "</div>", unsafe_allow_html=True)

    _scheme_hits = _scheme_decisions_for_rows(ready, sup_id, sup_name, doc_date)
    _scheme_blocks = [x for x in _scheme_hits if x.get("block")]
    if _scheme_hits:
        with st.expander("🧾 Supplier Scheme Validation", expanded=bool(_scheme_blocks)):
            for hit in _scheme_hits:
                row = hit["row"]
                target = hit["target"]
                msg = hit["message"]
                prefix = "❌" if hit.get("block") else "✅"
                line = (
                    f"{prefix} {target.get('order_no') or 'Order'} · "
                    f"{row.get('product_name') or row.get('supplier_item') or 'Line'} · {msg}"
                )
                if hit.get("block"):
                    st.error(line)
                else:
                    st.success(line)
            if _scheme_blocks:
                st.caption(
                    "Posting is blocked because supplier procurement price is above "
                    "the active scheme rate. Correct the invoice rate or update the scheme."
                )

    st.markdown("---")
    ck = "imui_p3_armed"
    if not st.session_state.get(ck):
        c1, c2 = st.columns([1,3])
        if c1.button("← Back", key="imui_p3_back"):
            _p("imui_phase",2); st.rerun()
        if c2.button(
            f"📤 Post {len(ready)} Line(s) · ₹{total:,.2f}",
            type="primary", key="imui_p3_post", disabled=bool(_scheme_blocks)):
            st.session_state[ck] = True; st.rerun()
    else:
        st.error(f"Confirm posting **{inv_no}** from **{sup_name}** · "
                  f"{len(ready)} line(s) · ₹{total:,.2f}?")
        y1, y2 = st.columns(2)
        if y1.button("✕ Cancel", key="imui_p3_no"):
            del st.session_state[ck]; st.rerun()
        if y2.button("✅ Confirm Post", type="primary", key="imui_p3_yes"):
            _atomic_post(ready, sup_id, sup_name, inv_no, doc_date, doc_type, path)


def _match_order_lines_for_item(row: dict) -> list:
    """
    Find procurement queue order_line_ids matching this invoice row.
    For Bonzer: matches product_id + power (sph/cyl/axis/add) + index_value + coating.
    For others: matches product_id + power only.
    """
    pid = str(row.get("product_id") or "")
    if not pid:
        return []
    pwr   = row.get("power_json") or {}
    r_pwr = pwr.get("right") or {}
    l_pwr = pwr.get("left")  or {}
    flat  = {k: row.get(k) for k in ("sph","cyl","axis","add") if row.get(k) is not None}

    # Extract Bonzer index/coating from description for structured queue match
    _idx_val  = None
    _coat_val = None
    _desc = str(row.get("supplier_item") or row.get("description") or "")
    _row_coat = str(row.get("coating") or "").upper()
    if _desc:
        try:
            from modules.procurement.supplier_invoice_rules import parse_bonzer_description
            _bz = parse_bonzer_description(_desc)
            _idx_val  = _bz.get("index")        # e.g. 1.56
            _coat_val = _row_coat or str(_bz.get("treatment") or _bz.get("coating") or "").upper()
        except Exception:
            pass

    def _eye_rows(eye, p, strict_queue: bool = True):
        conds  = ["ol.product_id=%(pid)s::uuid",
                  "ol.eye_side=%(eye)s",
                  "COALESCE(ol.is_deleted,FALSE)=FALSE",
                  "NOT EXISTS (SELECT 1 FROM purchase_acknowledgements pa "
                  " WHERE pa.order_line_id=ol.id "
                  " AND pa.billing_status NOT IN ('VOID','CANCELLED'))"]
        if strict_queue:
            conds.append(
                "(ol.lens_params->>'replenishment_status' IN ('ORDERED',"
                " 'SUPPLIER_CONFIRMED') OR "
                " ol.lens_params->>'supplier_stage' IN "
                " ('SUPPLIER_CONFIRMED','AWAITING_SUPPLY','ORDER_PLACED'))"
            )
        params = {"pid": pid, "eye": eye}

        # Power conditions
        for col, key in [("sph","sph"),("cyl","cyl"),("axis","axis"),("add_power","add")]:
            val = p.get(key)
            if val is None: continue
            fval = float(val)
            if col == "axis":
                if int(fval) > 0:
                    conds.append(f"COALESCE(ol.{col},0)=%(ax)s")
                    params["ax"] = int(fval)
            else:
                conds.append(f"ABS(COALESCE(ol.{col},0)-%(_{col})s)<0.03")
                params[f"_{col}"] = fval

        # Index match via lens_params (Bonzer / ophthalmic lenses)
        if _idx_val:
            conds.append(
                "(ol.lens_params->>'lens_index' IS NULL "
                " OR ABS(COALESCE((ol.lens_params->>'lens_index')::numeric,0)"
                "        -%(idx_v)s)<0.01)"
            )
            params["idx_v"] = _idx_val

        # Coating match via lens_params
        if _coat_val and _coat_val not in ("", "CLEAR"):
            conds.append(
                "(ol.lens_params->>'coating' IS NULL "
                " OR UPPER(ol.lens_params->>'coating') LIKE %(coat_v)s)"
            )
            params["coat_v"] = f"%{_coat_val[:4]}%"  # e.g. %BLUE%

        sql = ("SELECT ol.id::text AS line_id, ol.eye_side, o.order_no, "
               "o.party_id::text AS party_id, COALESCE(p.brand,'') AS brand, "
               "COALESCE(p.product_name,'') AS product_name "
               "FROM order_lines ol JOIN orders o ON o.id=ol.order_id "
               "LEFT JOIN products p ON p.id = ol.product_id "
               f"WHERE {' AND '.join(conds)} "
               "ORDER BY o.created_at DESC LIMIT 3")
        try:
            return [{"order_line_id": r["line_id"], "eye_side": r["eye_side"],
                     "order_no": r["order_no"], "party_id": r.get("party_id") or "",
                     "brand": r.get("brand") or "", "product_name": r.get("product_name") or ""}
                    for r in (_q(sql, params) or [])]
        except Exception as e:
            import logging; logging.getLogger(__name__).debug("[match_q] %s", e)
            return []

    results = []
    if r_pwr: results += _eye_rows("R", r_pwr, True)
    if l_pwr: results += _eye_rows("L", l_pwr, True)
    if not results and flat:
        results += _eye_rows("R", flat, True)
        results += _eye_rows("L", flat, True)
    if not results:
        if r_pwr: results += _eye_rows("R", r_pwr, False)
        if l_pwr: results += _eye_rows("L", l_pwr, False)
        if not results and flat:
            results += _eye_rows("R", flat, False)
            results += _eye_rows("L", flat, False)
    return results


def _mark_lines_procured(order_line_ids: list, pa_id: str):
    """Update order_lines to PROCURED so procurement queue clears after invoice posting."""
    for lid in order_line_ids:
        try:
            _w("""UPDATE order_lines
                  SET lens_params = jsonb_set(
                      jsonb_set(
                          jsonb_set(COALESCE(lens_params,'{}'::jsonb),
                                    '{replenishment_status}', '"PROCURED"'),
                          '{supplier_stage}', '"READY_FOR_BILLING"'),
                      '{procurement_status}', '"PROCURED"')
                  WHERE id=%(lid)s::uuid AND COALESCE(is_deleted,FALSE)=FALSE""",
               {"lid": lid})
            if pa_id:
                _w("UPDATE purchase_acknowledgements SET order_line_id=%(lid)s::uuid "
                   "WHERE id=%(pa)s::uuid AND order_line_id IS NULL",
                   {"lid": lid, "pa": pa_id})
        except Exception as e:
            import logging; logging.getLogger(__name__).warning("[procured] %s", e)


def _audit_status_for_pa(target_line_id: str | None, product_name: str, supplier_item: str) -> str:
    if target_line_id:
        return "LINKED_PROCUREMENT"
    _txt = f"{product_name or ''} {supplier_item or ''}".lower()
    if "courier" in _txt or "freight" in _txt:
        return "DIRECT_COST"
    return "PENDING_INVENTORY_AUDIT"


def _supplier_order_ref_from_row(row: dict) -> str:
    """Best-effort supplier-side job/order ref parsed from invoice rows."""
    for key in (
        "supplier_order_ref", "supplier_order_no", "supplier_ref",
        "supplier_job_no", "job_no", "order_no_supplier",
        "order_no_our", "our_order_no", "ref_no",
    ):
        val = str(row.get(key) or "").strip()
        if val:
            return val[:120]
    return ""


def _scheme_decisions_for_rows(rows: list, supplier_id: str, supplier_name: str,
                               doc_date: str | None = None) -> list[dict]:
    """Evaluate active supplier-party schemes against invoice rows."""
    decisions = []
    try:
        from modules.pricing.supplier_scheme_engine import evaluate_scheme, describe_decision
    except Exception:
        return decisions

    for row in rows:
        targets = _match_order_lines_for_item(row)
        if not targets:
            continue
        target_count = max(len(targets), 1)
        line_total = round(
            float(row.get("taxable_value") or (float(row.get("qty") or 0) * float(row.get("unit_price") or 0))),
            2,
        )
        split_total = round(line_total / target_count, 2)
        split_qty = 1.0 if targets and targets[0] is not None else float(row.get("qty") or 1)
        split_price = round(split_total / max(split_qty, 0.0001), 2)
        for target in targets:
            ctx = {
                "supplier_id": supplier_id or "",
                "supplier_name": supplier_name or "",
                "party_id": target.get("party_id") or "",
                "product_id": row.get("product_id") or "",
                "product_name": target.get("product_name") or row.get("product_name") or "",
                "brand": target.get("brand") or row.get("brand") or "",
                "lens_index": row.get("lens_index") or row.get("index") or "",
                "coating": row.get("coating") or "",
                "treatment": row.get("treatment") or "",
                "design": row.get("design") or row.get("product_name") or "",
                "procurement_unit_price": split_price,
                "base_procurement_price": row.get("unit_price"),
                "received_qty": row.get("qty"),
                "taxable_value": row.get("taxable_value") or line_total,
                "invoice_unit_price": row.get("unit_price"),
                "date": doc_date,
            }
            decision = evaluate_scheme(ctx)
            if decision.matched:
                decisions.append({
                    "row": row,
                    "target": target,
                    "decision": decision,
                    "message": describe_decision(decision),
                    "block": not decision.procurement_ok,
                })
    return decisions


def _audit_log_insert(cur, pa_id: str, action: str, from_status: str, to_status: str,
                      order_line_id: str | None, invoice_no: str, supplier_name: str,
                      remarks: str = ""):
    cur.execute("""
        INSERT INTO procurement_pa_audit_log (
            pa_id, action, from_status, to_status, order_line_id,
            invoice_no, supplier_name, remarks, performed_by, performed_at
        ) VALUES (
            %s::uuid, %s, NULLIF(%s,''), NULLIF(%s,''),
            NULLIF(%s,'')::uuid, NULLIF(%s,''), %s, NULLIF(%s,''),
            %s, NOW()
        )
    """, (
        pa_id, action, from_status or "", to_status or "",
        order_line_id or "", invoice_no or "", supplier_name or "",
        remarks or "", _actor_name(),
    ))


def _power_stock_rows(row: dict, qty: float) -> list[dict]:
    pwr = row.get("power_json") or {}
    rows = []
    eye_powers = [
        ("R", pwr.get("r")) if isinstance(pwr, dict) else ("R", None),
        ("L", pwr.get("l")) if isinstance(pwr, dict) else ("L", None),
    ]
    valid_eye_powers = [(side, data) for side, data in eye_powers if isinstance(data, dict) and data]
    each_qty = max(1, int(round((qty or 1) / max(len(valid_eye_powers), 1))))
    for side, data in valid_eye_powers:
        rows.append({
            "eye": side, "qty": each_qty,
            "sph": data.get("sph"), "cyl": data.get("cyl"),
            "axis": data.get("axis"), "add": data.get("add"),
        })
    if rows:
        return rows
    if isinstance(pwr, dict):
        flat = {k: pwr.get(k) for k in ("sph", "cyl", "axis", "add")}
        if any(v not in (None, "", 0, 0.0) for v in flat.values()):
            return [{"eye": "", "qty": max(1, int(round(qty or 1))), **flat}]
    return [{"eye": "", "qty": max(1, int(round(qty or 1))), "sph": None, "cyl": None, "axis": None, "add": None}]


def _post_orphan_inventory(cur, pa_id: str, row: dict, qty: float, price: float,
                           sup_id: str, sup_name: str, inv_no: str):
    """Add unmatched goods to inventory and queue the PA for owner audit."""
    if not row.get("product_id"):
        return
    product_name = str(row.get("product_name") or row.get("supplier_item") or "")
    supplier_item = str(row.get("supplier_item") or "")
    if _audit_status_for_pa(None, product_name, supplier_item) == "DIRECT_COST":
        return
    batch = row.get("batch_no") or f"INV-{inv_no or pa_id[:8]}"
    expiry = row.get("expiry_date") or None
    rate = float(price or row.get("unit_price") or 0)
    for stock_row in _power_stock_rows(row, qty):
        cur.execute("""
            INSERT INTO inventory_stock (
                product_id, sph, cyl, axis, add_power, eye_side,
                batch_no, expiry_date, quantity,
                purchase_rate, purchase_price, selling_price,
                stock_type, item_type, is_active,
                supplier_id, supplier_name,
                coating, index_value, treatment,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, NULLIF(%s,''),
                NULLIF(%s,''), NULLIF(%s,'')::date, %s,
                %s, %s, %s,
                'BATCH', 'STOCK', TRUE,
                NULLIF(%s,'')::uuid, %s,
                NULLIF(%s,''), NULLIF(%s,'')::numeric, NULLIF(%s,''),
                NOW(), NOW()
            )
        """, (
            row.get("product_id"),
            stock_row.get("sph"), stock_row.get("cyl"), stock_row.get("axis"), stock_row.get("add"),
            stock_row.get("eye") or "",
            batch, expiry, int(stock_row.get("qty") or 1),
            rate, rate, rate,
            sup_id or "", sup_name or "",
            row.get("coating") or "", row.get("lens_index") or "", row.get("treatment") or "",
        ))
    cur.execute("""
        UPDATE purchase_acknowledgements
        SET inventory_posted_at = NOW()
        WHERE id = %s::uuid
          AND inventory_posted_at IS NULL
    """, (pa_id,))
    _audit_log_insert(
        cur, pa_id, "ORPHAN_INVENTORY_POSTED", "",
        "PENDING_INVENTORY_AUDIT", None, inv_no, sup_name,
        "Unlinked invoice line added to inventory and queued for owner audit.",
    )


def _atomic_post(ready, sup_id, sup_name, inv_no, doc_date, doc_type, path):
    """Single transaction — all rows or none."""
    try:
        from modules.core.date_guard import validate_not_future
        _ok_dt, _msg_dt = validate_not_future(doc_date, "Supplier document date")
    except Exception as _dg_e:
        _ok_dt, _msg_dt = False, f"Date validation failed: {_dg_e}"
    if not _ok_dt:
        st.error(_msg_dt)
        return

    chal = inv_no if doc_type in ("CHALLAN","BOTH") else None
    inv  = inv_no if doc_type in ("INVOICE","BOTH") else None
    ddate = doc_date.replace("/","-").replace(".","-") if doc_date else None
    totals = (_g("imui_parsed", {}) or {}).get("totals") or {}
    courier_amt = float(totals.get("courier_charges") or totals.get("courier") or 0)
    courier_gst_rate = 18.0
    courier_gst_amount = round(courier_amt * courier_gst_rate / 100, 2) if courier_amt else 0.0

    _scheme_blocks = [
        x for x in _scheme_decisions_for_rows(ready, sup_id, sup_name, doc_date)
        if x.get("block")
    ]
    if _scheme_blocks:
        st.error(
            "Supplier scheme validation failed. Correct the invoice rate or "
            "update the active scheme before posting."
        )
        for _hit in _scheme_blocks[:5]:
            st.error(_hit.get("message") or "Scheme violation")
        return

    # Build match plan before opening the write transaction. One supplier
    # invoice row can represent both R and L powers (Bonzer pair format);
    # purchase_acknowledgements is one order_line_id per row, so split the
    # invoice value across matched order lines and insert one PA per line.
    _match_plan = []
    for _r in ready:
        _matches = _match_order_lines_for_item(_r)
        _targets = _matches or [None]
        _match_plan.append((_r, _targets))

    try:
        from modules.sql_adapter import get_transaction_connection
        conn = get_transaction_connection()
    except Exception as exc:
        st.error(f"Cannot open DB transaction: {exc}"); return

    try:
        with conn.cursor() as cur:
            # Race-condition-safe final duplicate check inside transaction
            cur.execute(
                "SELECT COUNT(*) FROM purchase_acknowledgements "
                "WHERE LOWER(COALESCE(invoice_no,''))=LOWER(%s) "
                "AND (%s='' OR supplier_id=%s::uuid)",
                (inv_no, sup_id or "", sup_id or "00000000-0000-0000-0000-000000000000"))
            if cur.fetchone()[0] > 0:
                raise ValueError(f"Duplicate invoice {inv_no} — aborted.")

            _posted_count = 0
            _cleared = 0
            for r, targets in _match_plan:
                _target_count = max(len(targets), 1)
                _line_total = round(
                    float(r.get("taxable_value") or (float(r["qty"]) * float(r["unit_price"]))),
                    2,
                )
                _split_total = round(_line_total / _target_count, 2)
                _split_qty = float(r["qty"]) / _target_count
                if targets[0] is not None:
                    # Procurement order lines are normally one lens/one CL line.
                    # Keep the line quantity practical and split the price value.
                    _split_qty = 1.0
                _split_price = round(_split_total / max(_split_qty, 0.0001), 2)

                for _target in targets:
                    _target_line_id = (
                        _target.get("order_line_id")
                        if isinstance(_target, dict) else None
                    )
                    _target_order_no = (
                        _target.get("order_no")
                        if isinstance(_target, dict) else None
                    )
                    _target_eye = (
                        _target.get("eye_side")
                        if isinstance(_target, dict) else None
                    )
                    _pa_audit_status = _audit_status_for_pa(
                        _target_line_id,
                        r.get("product_name",""),
                        r.get("supplier_item",""),
                    )
                    _supplier_order_ref = _supplier_order_ref_from_row(r) or inv_no
                    cur.execute("""
                        INSERT INTO purchase_acknowledgements (
                            order_line_id, order_no,
                            supplier_id, supplier_name,
                            challan_no, invoice_no, document_date,
                            received_qty, purchase_price, total_value,
                        batch_no, expiry_date, invoice_file_path,
                        courier_gst_rate, courier_gst_amount,
                        eye_side,
                        our_product_name, our_product_id,
                        supplier_product_name, supplier_product_description,
                        supplier_order_ref,
                        mapping_source,
                            billing_status, invoice_match_state,
                            audit_status, audit_remarks,
                            acknowledged_at
                        ) VALUES (
                            NULLIF(%s,'')::uuid, NULLIF(%s,''),
                            NULLIF(%s,'')::uuid, %s,
                            NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,'')::date,
                            %s, %s, %s,
                            NULLIF(%s,''), NULLIF(%s,'')::date, NULLIF(%s,''),
                            %s, %s,
                            NULLIF(%s,''),
                        %s, NULLIF(%s,'')::uuid,
                        %s, %s, NULLIF(%s,''),
                            'invoice_match_ui',
                            'PURCHASE_ACKED', 'POSTED',
                            %s, %s,
                            NOW()
                        )
                        RETURNING id::text
                    """, (
                        _target_line_id or "", _target_order_no or "",
                        sup_id or "", sup_name,
                        chal, inv, ddate,
                        _split_qty, _split_price, _split_total,
                        r.get("batch_no") or None,
                        r.get("expiry_date") or None,
                        path or None,
                        float(r.get("gst_percent") or 0),
                        float(r.get("gst_amount") or 0) / max(_target_count, 1),
                        _target_eye or "",
                        r.get("product_name",""),
                        r.get("product_id") or None,
                        r.get("supplier_item",""),
                        " · ".join(filter(None, [
                            r.get("supplier_item",""),
                            f"Index {r.get('lens_index')}" if r.get("lens_index") else "",
                            r.get("coating","") or "",
                            r.get("treatment","") or "",
                            f"Discount {float(r.get('discount') or 0):.2f}" if float(r.get("discount") or 0) else "",
                            f"GST {float(r.get('gst_percent') or 0):.2f}% = {float(r.get('gst_amount') or 0):.2f}" if float(r.get("gst_amount") or 0) else "",
                        ])),
                        _supplier_order_ref,
                        _pa_audit_status,
                        "Linked to procurement queue" if _target_line_id else "No procurement queue match; added to inventory audit",
                    ))
                    _pa_id = cur.fetchone()[0]
                    _posted_count += 1
                    if _target_line_id:
                        _audit_log_insert(
                            cur, _pa_id, "LINKED_PROCUREMENT", "",
                            "LINKED_PROCUREMENT", _target_line_id,
                            inv_no, sup_name,
                            f"Invoice match linked to order {_target_order_no or ''}.",
                        )
                    else:
                        _audit_log_insert(
                            cur, _pa_id, "QUEUED_FOR_PURCHASE_REGISTER", "",
                            "PENDING_INVENTORY_AUDIT", None, inv_no, sup_name,
                            "Unlinked invoice-match line queued for Purchase Register review.",
                        )
                    if _target_line_id:
                        cur.execute("""
                            UPDATE order_lines
                            SET lens_params = jsonb_set(
                                jsonb_set(
                                    jsonb_set(COALESCE(lens_params,'{}'::jsonb),
                                              '{replenishment_status}', '"PROCURED"'),
                                    '{supplier_stage}', '"READY_FOR_BILLING"'),
                                '{procurement_status}', '"PROCURED"')
                            WHERE id=%s::uuid
                              AND COALESCE(is_deleted,FALSE)=FALSE
                        """, (_target_line_id,))
                        cur.execute("""
                            UPDATE purchase_acknowledgements
                            SET order_line_id=%s::uuid
                            WHERE id=%s::uuid
                              AND order_line_id IS NULL
                        """, (_target_line_id, _pa_id))
                        _cleared += 1

            if courier_amt:
                cur.execute("""
                    INSERT INTO purchase_acknowledgements (
                        supplier_id, supplier_name,
                        challan_no, invoice_no, document_date,
                        received_qty, purchase_price, total_value,
                        invoice_file_path,
                        courier_gst_rate, courier_gst_amount,
                        our_product_name, supplier_product_name,
                        supplier_product_description,
                        mapping_source,
                        billing_status, invoice_match_state,
                        audit_status, audit_remarks,
                        acknowledged_at
                    ) VALUES (
                        NULLIF(%s,'')::uuid, %s,
                        NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,'')::date,
                        1, %s, %s,
                        NULLIF(%s,''),
                        %s, %s,
                        'Courier / Freight', 'Courier / Freight',
                        %s,
                        'invoice_match_ui',
                        'PURCHASE_ACKED', 'POSTED',
                        'DIRECT_COST', 'Courier/Freight direct cost',
                        NOW()
                    )
                    RETURNING id::text
                """, (
                    sup_id or "", sup_name,
                    chal, inv, ddate,
                    courier_amt, courier_amt,
                    path or None,
                    courier_gst_rate, courier_gst_amount,
                    f"Courier/Freight · GST {courier_gst_rate:.2f}% = {courier_gst_amount:.2f}",
                ))
                _courier_pa_id = cur.fetchone()[0]
                _audit_log_insert(
                    cur, _courier_pa_id, "DIRECT_COST_POSTED", "",
                    "DIRECT_COST", None, inv_no, sup_name,
                    "Courier/Freight line posted from supplier invoice totals.",
                )
                _posted_count += 1

        conn.commit()

        # Bust queue caches
        for k in [x for x in st.session_state if x.startswith("_prx_rows_")]:
            del st.session_state[k]
        _msg = f"✅ {_posted_count} purchase line(s) posted atomically."
        if _cleared:
            _msg += f" {_cleared} procurement queue line(s) marked Inventory Movement."
        st.success(_msg)
        # Navigate directly to Inventory Movement tab on next render.
        # Do not write prod_lazy_panel directly after its widget is instantiated.
        st.session_state["_prod_lazy_panel_next"] = "📊 Inventory Movement"
        _p("imui_phase", 4); st.rerun()

    except Exception as exc:
        conn.rollback()
        st.error(f"❌ Post failed — fully rolled back. Nothing was saved.\n\n{exc}")
    finally:
        try: conn.close()
        except Exception: pass


def _write_audit_log(rows: list, invoice_no: str, sup_name: str):
    """Write one audit row per matched line — records AI suggestion vs user choice."""
    for r in rows:
        ai_pid  = r.get("product_id","")     # after override, this is user's choice
        ai_conf = float(r.get("ai_confidence") or 0)
        # was_override: True when user changed AI suggestion (confidence was < 0.95)
        was_override = not r.get("approved", False) or ai_conf < 0.95
        _w("""
            INSERT INTO invoice_match_audit
                (invoice_no, supplier_name, invoice_line,
                 ai_product_name, ai_method, ai_confidence,
                 user_product_id, user_product_name,
                 operator, was_override, created_at)
            VALUES (
                NULLIF(%s,''), %s, %s,
                %s, %s, %s,
                NULLIF(%s,'')::uuid, %s,
                %s, %s, NOW()
            )
        """, (
            invoice_no, sup_name, r.get("supplier_item",""),
            r.get("product_name",""), r.get("ai_method",""), ai_conf,
            r.get("product_id","") or None, r.get("product_name",""),
            st.session_state.get("user_name","staff"), was_override,
        ))


def _phase4():
    st.success("🎉 Invoice posted successfully!")
    st.info("You are now in the **Inventory Movement** tab. "
             "Use Purchase Register if you need rollback or accounting corrections.")
    c1, c2 = st.columns(2)
    if c1.button("🔄 Process Another Invoice", key="imui_restart"):
        _reset()
        st.session_state["_prod_lazy_panel_next"] = "📄 Invoice Match"
        st.rerun()
    if c2.button("📋 Go to Purchase Register", type="primary", key="imui_go_reg"):
        st.session_state["_prod_lazy_panel_next"] = "📊 Inventory Movement"
        _reset()
        st.rerun()



# ── Normalisation ─────────────────────────────────────────────────────────────
def _normalise_items(parsed: Dict[str, Any], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw  = parsed.get("items") or []
    fmt  = str(rules.get("power_format","GENERIC")).upper()
    cs   = str(rules.get("cyl_sign","AS_WRITTEN")).upper()
    qu   = str(rules.get("qty_unit","PCS")).upper()
    out  = []
    for item in raw:
        desc  = str(item.get("description") or item.get("product_name") or "")
        qty   = float(item.get("qty_pcs") or item.get("qty") or 1)
        if qu=="PAIRS" and item.get("qty_pairs"): qty=float(item["qty_pairs"])*2
        price = float(item.get("unit_price_per_pc") or item.get("unit_price")
                      or item.get("rate") or 0)
        discount = float(item.get("discount") or item.get("discount_amount") or 0)
        gst_amount = float(item.get("gst") or item.get("gst_amount") or 0)
        taxable_value = float(item.get("taxable_value") or 0)
        pwr: Dict[str,Any] = {}
        if fmt=="MERGED_RL" and ("[R]" in desc.upper() or "[L]" in desc.upper()):
            pd = parse_power_merged_rl(desc)
            desc = pd.get("product_name", desc)
            pwr  = {"right": pd.get("right",{}), "left": pd.get("left",{})}
        elif fmt=="ALCON_TORIC" and ("ASTG" in desc.upper() or "TORIC" in desc.upper()):
            pwr = parse_power_alcon_toric(desc, cs)
        elif item.get("right") or item.get("left"):
            pwr = {"right": item.get("right",{}), "left": item.get("left",{})}
        else:
            pwr = {k: item[k] for k in ("sph","cyl","axis","add","bc","dia") if item.get(k) is not None}
        out.append({"description": desc,
                    "product_name": item.get("product_name") or desc,
                    "qty": qty, "unit_price": price,
                    "discount": discount,
                    "gst_amount": gst_amount,
                    "taxable_value": taxable_value,
                    "batch_no": str(item.get("batch_no") or ""),
                    "expiry_date": str(item.get("expiry_date") or ""),
                    "power": pwr})
    return out


def _fmt_power(pwr: Dict[str, Any]) -> str:
    parts = []
    if pwr.get("right") or pwr.get("left"):
        def e(p):
            b = []
            if p.get("sph") is not None: b.append(f"S{float(p['sph']):+.2f}")
            if p.get("cyl") is not None: b.append(f"C{float(p['cyl']):+.2f}")
            if p.get("axis"):             b.append(f"Ax{int(p['axis'])}")
            if p.get("add"):              b.append(f"+{float(p['add']):.2f}Add")
            return " ".join(b)
        if pwr.get("right"): parts.append(f"R:{e(pwr['right'])}")
        if pwr.get("left"):  parts.append(f"L:{e(pwr['left'])}")
    else:
        if pwr.get("sph")  is not None: parts.append(f"S{float(pwr['sph']):+.2f}")
        if pwr.get("cyl")  is not None: parts.append(f"C{float(pwr['cyl']):+.2f}")
        if pwr.get("axis"):              parts.append(f"Ax{int(pwr['axis'])}")
        if pwr.get("bc"):                parts.append(f"BC{pwr['bc']}")
        if pwr.get("dia"):               parts.append(f"Dia{pwr['dia']}")
    return " · ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIER TRAINING UI
# ═══════════════════════════════════════════════════════════════════════════════
def render_supplier_training_ui():
    _ensure_rules_table()
    st.markdown("### 🎓 Supplier Invoice AI Training")
    st.caption(
        "Rules improve matching accuracy over time. Each supplier has its own "
        "power format, CYL sign convention, product aliases, and regex patterns.")

    parties = _suppliers()
    sup_ids = [""] + [p["id"] for p in parties]
    sup_lbls = {"":"— Select —"}
    sup_lbls.update({p["id"]: p["party_name"] for p in parties})

    t1, t2 = st.tabs(["📋 All Rules", "➕ Add / Edit"])

    with t1:
        rows = list_all_rules()
        if not rows:
            st.info("No rules yet. Add the first in 'Add / Edit' tab.")
        for row in rows:
            r = row.get("rules") or {}
            if isinstance(r, str):
                try: r = json.loads(r)
                except Exception: r = {}
            with st.expander(
                f"🏭 {row['supplier_name']}  "
                f"(v{row.get('version',1)} · {str(row.get('updated_at',''))[:10]})",
                expanded=False):
                st.markdown(
                    f"**Power:** `{r.get('power_format','GENERIC')}` · "
                    f"**CYL:** `{r.get('cyl_sign','AS_WRITTEN')}` · "
                    f"**Qty:** `{r.get('qty_unit','PCS')}`")
                if r.get("notes"): st.caption(r["notes"])
                for k,v in (r.get("product_aliases") or {}).items():
                    st.markdown(f"  `{k}` → **{v}**")
                if st.button("✏️ Edit", key=f"edit_{row['supplier_name'][:15]}"):
                    st.session_state["_train_edit"] = row; st.rerun()

    with t2:
        er = st.session_state.pop("_train_edit", {})
        exr = er.get("rules") or {}
        if isinstance(exr, str):
            try: exr = json.loads(exr)
            except Exception: exr = {}

        sel = st.selectbox("Supplier", sup_ids, format_func=lambda x: sup_lbls.get(x,x),
                            key="train_sup")
        sname = sup_lbls.get(sel,"") or st.text_input(
            "Or type new supplier name", key="train_name")

        c1,c2,c3 = st.columns(3)
        fmt  = c1.selectbox("Power Format",
                             ["GENERIC","ALCON_TORIC","BONZER_RL","MERGED_RL","SIMPLE"],
                             index=["GENERIC","ALCON_TORIC","BONZER_RL","MERGED_RL","SIMPLE"]
                             .index(exr.get("power_format","GENERIC")), key="train_fmt")
        cyl  = c2.selectbox("CYL Sign", ["AS_WRITTEN","ALWAYS_NEGATIVE"],
                             index=["AS_WRITTEN","ALWAYS_NEGATIVE"]
                             .index(exr.get("cyl_sign","AS_WRITTEN")), key="train_cyl")
        qty  = c3.selectbox("Qty Unit", ["PCS","PAIRS","BOX"],
                             index=["PCS","PAIRS","BOX"]
                             .index(exr.get("qty_unit","PCS")), key="train_qty")

        aliases_txt = st.text_area(
            "Product Aliases  (INVOICE TEXT → Our Product Name)",
            value="\n".join(f"{k} → {v}" for k,v in
                            (exr.get("product_aliases") or {}).items()),
            key="train_alias", height=110,
            placeholder="AIROPTIX AQ HG SPH → Air Optix Hydraglyde SPH 6PK\n"
                        "AIROPT ASTG HG → Air Optix Toric")

        inv_pat  = st.text_input("Invoice No regex (group 1)",
                                  value=(exr.get("field_patterns") or {}).get("invoice_no",""),
                                  key="train_ipat", placeholder=r"\b(9\d{9})\b")
        date_pat = st.text_input("Date regex (group 1)",
                                  value=(exr.get("field_patterns") or {}).get("date",""),
                                  key="train_dpat",
                                  placeholder=r"Date\s*\|?\s*(\d{2}\.\d{2}\.\d{4})")
        notes    = st.text_area(
            "Training Notes (describe invoice quirks for the AI)",
            value=exr.get("notes",""), key="train_notes", height=85,
            placeholder="Alcon: CYL always negative. Batch 8-digit / DD.MM.YYYY. HSN 90013000.")

        if st.button("💾 Save Rule", type="primary", key="train_save",
                      disabled=not sname.strip()):
            aliases = {}
            for line in (aliases_txt or "").splitlines():
                if "→" in line:
                    k,_,v = line.partition("→")
                    if k.strip() and v.strip(): aliases[k.strip()] = v.strip()
            ok = save_rules(sname.strip(), {
                "power_format": fmt, "cyl_sign": cyl, "qty_unit": qty,
                "product_aliases": aliases,
                "field_patterns": {"invoice_no": inv_pat.strip() or None,
                                   "date": date_pat.strip() or None},
                "notes": notes.strip(),
            }, sel or "", st.session_state.get("user_name","staff"))
            if ok: st.success(f"✅ Saved for {sname}."); st.rerun()
            else:  st.error("Save failed.")


# ── Main entry ────────────────────────────────────────────────────────────────
def render_invoice_match_ui():
    _ensure_rules_table()
    phase = _g("imui_phase", 1)
    _bar(phase)
    if   phase==1: _phase1()
    elif phase==2: _phase2()
    elif phase==3: _phase3()
    elif phase==4: _phase4()
    else: _reset(); st.rerun()

    if phase > 1:
        st.markdown("---")
        if st.button("↩ Start over", key="imui_reset"):
            _reset(); st.rerun()
