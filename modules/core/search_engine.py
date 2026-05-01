"""
modules/core/search_engine.py
================================
Shared fuzzy search engine for DV ERP.
Used by: retail_punching, wholesale_punching, bulk_order,
         payment_collection, inventory, procurement.

Features:
  - RapidFuzz (typo-tolerant) with substring fallback
  - @st.cache_data cached loaders (no DB hit per keystroke)
  - Unified party/patient/product search
  - Click-to-select UI widget (no confirm button)
  - 1-char trigger, results in <50ms
"""

import streamlit as st
import logging
from typing import List, Dict, Optional

_log = logging.getLogger(__name__)

try:
    from rapidfuzz import process as _rfp, fuzz as _rff
    _HAS_RF = True
except ImportError:
    _HAS_RF = False


# ── DB helper ─────────────────────────────────────────────────────────────────

def _q(sql, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params or ()) or []
    except Exception as e:
        _log.warning(f"[search_engine._q] {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# CACHED DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def load_parties_cache(ptype: str = "All") -> List[Dict]:
    """
    Load parties + patients into memory, cache 5 min.
    ptype: 'All' | 'Retail' | 'Wholesale'
    """
    rows = []
    try:
        if ptype == "Wholesale":
            type_filter = "AND UPPER(COALESCE(party_type,'WHOLESALE')) NOT IN ('RETAIL','DOCTOR')"
        elif ptype == "Retail":
            type_filter = "AND UPPER(COALESCE(party_type,'RETAIL')) IN ('RETAIL','DOCTOR','')"
        else:
            type_filter = ""

        rows = _q("""
            SELECT id::text, party_name,
                   COALESCE(mobile,'')      AS mobile,
                   COALESCE(city,'')        AS city,
                   COALESCE(party_type,'')  AS party_type,
                   COALESCE(gstin,'')       AS gstin,
                   COALESCE(credit_limit,0) AS credit_limit,
                   COALESCE(billing_category,'ON_COMPLETION') AS billing_category,
                   'PARTY'                  AS record_type
            FROM parties
            WHERE COALESCE(is_active,TRUE) = TRUE
            {}
            ORDER BY party_name LIMIT 5000
        """.format(type_filter))
    except Exception:
        pass

    # Patients (retail only)
    if ptype in ("Retail", "All"):
        try:
            pts = _q("""
                SELECT id::text,
                       COALESCE(master_name,'') AS party_name,
                       COALESCE(mobile,'')      AS mobile,
                       ''                       AS city,
                       'RETAIL'                 AS party_type,
                       ''                       AS gstin,
                       0                        AS credit_limit,
                       'ADVANCE_BALANCE'         AS billing_category,
                       'PATIENT'                AS record_type
                FROM patients
                ORDER BY master_name LIMIT 5000
            """)
            seen = {r["id"] for r in rows}
            rows += [r for r in pts if r["id"] not in seen]
        except Exception:
            pass

    # Build search key
    for r in rows:
        r["search_key"] = " ".join(filter(None, [
            str(r.get("party_name","") or ""),
            str(r.get("mobile","") or ""),
            str(r.get("city","") or ""),
            str(r.get("gstin","") or ""),
        ])).lower()

    return rows


@st.cache_data(ttl=120, show_spinner=False)
def load_products_cache(active_only: bool = True) -> List[Dict]:
    """Load all products into memory, cache 2 min."""
    try:
        where = "WHERE COALESCE(is_active,TRUE)=TRUE" if active_only else ""
        rows = _q("""
            SELECT id::text, product_name, brand,
                   COALESCE(main_group,'') AS main_group,
                   COALESCE(category,'')  AS category,
                   COALESCE(unit,'PCS')   AS unit,
                   COALESCE(box_size,1)   AS box_size,
                   COALESCE(gst_percent,0) AS gst_percent,
                   COALESCE(hsn_code,'')  AS hsn_code,
                   COALESCE(is_batch_applicable,TRUE) AS is_batch_applicable
            FROM products {}
            ORDER BY product_name LIMIT 10000
        """.format(where))
        for r in rows:
            r["search_key"] = " ".join(filter(None, [
                str(r.get("product_name","") or ""),
                str(r.get("brand","") or ""),
                str(r.get("main_group","") or ""),
                str(r.get("category","") or ""),
            ])).lower()
        return rows
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# FUZZY SEARCH FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def fuzzy_search_parties(
    term: str,
    ptype: str = "All",
    limit: int = 12,
    score_cutoff: int = 35,
) -> List[Dict]:
    """
    Search parties + patients with RapidFuzz typo tolerance.
    Falls back to substring if rapidfuzz unavailable.
    """
    t = (term or "").strip()
    if not t:
        return []

    candidates = load_parties_cache(ptype)
    if not candidates:
        return []

    if _HAS_RF:
        keys    = [p["search_key"] for p in candidates]
        matches = _rfp.extract(
            t.lower(), keys,
            scorer=_rff.WRatio,
            limit=limit,
            score_cutoff=score_cutoff,
        )
        return [candidates[m[2]] for m in matches]

    tl = t.lower()
    return [p for p in candidates if tl in p["search_key"]][:limit]


def fuzzy_search_products(
    term: str,
    limit: int = 15,
    score_cutoff: int = 30,
) -> List[Dict]:
    """
    Search products with RapidFuzz typo tolerance.
    """
    t = (term or "").strip()
    if not t:
        return []

    candidates = load_products_cache()
    if not candidates:
        return []

    if _HAS_RF:
        keys    = [p["search_key"] for p in candidates]
        matches = _rfp.extract(
            t.lower(), keys,
            scorer=_rff.WRatio,
            limit=limit,
            score_cutoff=score_cutoff,
        )
        return [candidates[m[2]] for m in matches]

    tl = t.lower()
    return [p for p in candidates if tl in p["search_key"]][:limit]


# ══════════════════════════════════════════════════════════════════════════════
# REUSABLE UI WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

def party_search_widget(
    key: str,
    ptype: str = "All",
    placeholder: str = "Search name · mobile · city (typos OK)",
    label: str = "🔍 Select Party / Patient",
) -> Optional[Dict]:
    """
    Drop-in party search widget.
    Returns selected party dict or None.

    Usage:
        party = party_search_widget(key="retail_party", ptype="Retail")
        if party:
            pid   = party["id"]
            pname = party["party_name"]
    """
    sk = key + "_party_sel"

    # Already selected
    selected = st.session_state.get(sk)
    if selected:
        pn  = str(selected.get("party_name",""))
        mob = str(selected.get("mobile","") or "")
        pt  = str(selected.get("party_type","") or selected.get("record_type",""))
        col_a, col_b = st.columns([6, 1])
        with col_a:
            ini = "".join(w[0].upper() for w in pn.split()[:2]) or "?"
            sub = "  ·  ".join(filter(None, [mob, selected.get("city",""), pt]))
            st.markdown(
                f"<div style='background:#0d2818;border:1px solid #22c55e;"
                f"border-radius:8px;padding:8px 14px;display:flex;align-items:center;gap:12px'>"
                f"<span style='background:#16a34a;color:#fff;border-radius:50%;width:32px;"
                f"height:32px;display:flex;align-items:center;justify-content:center;"
                f"font-weight:700;font-size:.9rem;flex-shrink:0'>{ini}</span>"
                f"<div><div style='color:#4ade80;font-weight:700'>{pn}</div>"
                f"<div style='color:#86efac;font-size:.72rem'>{sub}</div></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with col_b:
            if st.button("✕", key=key+"_clear", use_container_width=True):
                st.session_state.pop(sk, None)
                st.rerun()
        return selected

    # Search input
    if label:
        st.markdown(
            f"<div style='font-size:.7rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>"
            f"{label}</div>",
            unsafe_allow_html=True,
        )

    term = st.text_input(
        "Search",
        key=key+"_input",
        placeholder=placeholder,
        label_visibility="collapsed",
    )

    if term:
        results = fuzzy_search_parties(term, ptype)
        if results:
            st.caption(f"{len(results)} result(s) — click to select:")
            for res in results:
                pn_r  = str(res.get("party_name","") or "—")
                mob_r = str(res.get("mobile","") or "")
                pt_r  = str(res.get("party_type","") or res.get("record_type",""))
                lbl   = pn_r
                if mob_r: lbl += f"  ·  {mob_r}"
                if pt_r:  lbl += f"  [{pt_r}]"
                if st.button(lbl, key=f"{key}_opt_{res['id']}", use_container_width=True):
                    # Enrich with full DB record if party (not patient)
                    if res.get("record_type") == "PARTY":
                        pr = _q(
                            "SELECT id::text, party_name, "
                            "COALESCE(mobile,'') AS mobile, "
                            "COALESCE(city,'') AS city, "
                            "COALESCE(party_type,'') AS party_type, "
                            "COALESCE(gstin,'') AS gstin, "
                            "COALESCE(credit_limit,0) AS credit_limit, "
                            "COALESCE(billing_category,'ON_COMPLETION') AS billing_category "
                            "FROM parties WHERE id=%s::uuid LIMIT 1",
                            (res["id"],)
                        )
                        if pr:
                            res = pr[0]
                            res["record_type"] = "PARTY"
                    st.session_state[sk] = res
                    # Clear the search input
                    st.session_state.pop(key+"_input", None)
                    st.rerun()
        else:
            st.caption(f"No results for '{term}' — try different spelling")
    else:
        st.markdown(
            "<div style='padding:16px;background:#0d1929;border:1px dashed #1e3a5f;"
            "border-radius:8px;color:#475569;font-size:.8rem;text-align:center'>"
            "Start typing — results appear instantly</div>",
            unsafe_allow_html=True,
        )

    return None


def product_search_widget(
    key: str,
    placeholder: str = "Search product / brand / category (typos OK)",
    label: str = "🔍 Select Product",
    show_clear: bool = True,
) -> Optional[Dict]:
    """
    Drop-in product search widget.
    Returns selected product dict or None.
    """
    sk = key + "_prod_sel"

    selected = st.session_state.get(sk)
    if selected:
        pn = str(selected.get("product_name",""))
        br = str(selected.get("brand","") or "")
        mg = str(selected.get("main_group","") or "")
        col_a, col_b = st.columns([6, 1])
        with col_a:
            st.markdown(
                f"<div style='background:#0a1628;border:1px solid #3b82f6;"
                f"border-radius:8px;padding:8px 14px'>"
                f"<span style='color:#60a5fa;font-weight:700'>{pn}</span>"
                f"<span style='color:#475569;font-size:.75rem;margin-left:10px'>"
                f"{br}  ·  {mg}</span></div>",
                unsafe_allow_html=True,
            )
        if show_clear:
            with col_b:
                if st.button("✕", key=key+"_prod_clear", use_container_width=True):
                    st.session_state.pop(sk, None)
                    st.rerun()
        return selected

    if label:
        st.markdown(
            f"<div style='font-size:.7rem;font-weight:700;color:#475569;"
            f"text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>"
            f"{label}</div>",
            unsafe_allow_html=True,
        )

    term = st.text_input(
        "Product search",
        key=key+"_prod_input",
        placeholder=placeholder,
        label_visibility="collapsed",
    )

    if term:
        results = fuzzy_search_products(term)
        if results:
            st.caption(f"{len(results)} product(s) found:")
            for res in results:
                pn_r = str(res.get("product_name",""))
                br_r = str(res.get("brand","") or "")
                mg_r = str(res.get("main_group","") or "")
                cat  = str(res.get("category","") or "")
                lbl  = f"{pn_r}"
                if br_r: lbl += f"  ·  {br_r}"
                if mg_r: lbl += f"  ·  {mg_r}"
                if cat:  lbl += f"  [{cat}]"
                if st.button(lbl, key=f"{key}_prod_{res['id']}", use_container_width=True):
                    st.session_state[sk] = res
                    st.session_state.pop(key+"_prod_input", None)
                    st.rerun()
        else:
            st.caption(f"No products found for '{term}'")
    else:
        st.markdown(
            "<div style='padding:12px;background:#0a1628;border:1px dashed #1e3a5f;"
            "border-radius:8px;color:#475569;font-size:.8rem;text-align:center'>"
            "Start typing to search products</div>",
            unsafe_allow_html=True,
        )

    return None


def invalidate_party_cache():
    """Call after adding/editing parties to force cache refresh."""
    load_parties_cache.clear()


def invalidate_product_cache():
    """Call after adding/editing products to force cache refresh."""
    load_products_cache.clear()
