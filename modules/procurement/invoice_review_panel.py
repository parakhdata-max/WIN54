"""
Editable scanned invoice review panel.

Flow:
  Load/Scan invoice -> OCR/PDF parse -> editable rows -> product dropdown
  -> purchase price from DB -> confirm -> save procurement ledger.
"""

from __future__ import annotations

import datetime as _dt
import difflib
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

from modules.procurement.invoice_image_ocr import parse_invoice_file
from modules.procurement.procurement_ledger import record_scanned_invoice_items
from modules.sql_adapter import run_query


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def _normalise_date(value: Any) -> str:
    """Return YYYY-MM-DD for DB date fields, or blank if the date is unclear."""
    v = str(value or "").strip()
    if not v:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date().isoformat()
        except Exception:
            pass
    return ""


@st.cache_data(ttl=120)
def _load_product_options() -> List[Dict[str, Any]]:
    return run_query(
        """
        SELECT
            p.id::text AS id,
            COALESCE(p.product_name, '') AS product_name,
            COALESCE(p.brand, '') AS brand,
            COALESCE(p.main_group, p.category, '') AS main_group,
            COALESCE(p.sku_code, p.product_code, p.barcode, '') AS sku_code,
            COALESCE(p.box_size, 1) AS box_size,
            COALESCE(p.is_batch_applicable, FALSE) AS is_batch_applicable,
            COALESCE((
                SELECT COALESCE(NULLIF(s.purchase_price,0), NULLIF(s.purchase_rate,0), 0)
                FROM inventory_stock s
                WHERE s.product_id = p.id
                  AND COALESCE(s.is_active, TRUE) = TRUE
                ORDER BY s.updated_at DESC NULLS LAST, s.created_at DESC NULLS LAST
                LIMIT 1
            ), 0) AS purchase_rate
        FROM products p
        WHERE COALESCE(p.is_active, TRUE) = TRUE
        ORDER BY p.brand, p.product_name
        LIMIT 5000
        """
    ) or []


def _label(p: Dict[str, Any]) -> str:
    brand = str(p.get("brand") or "").strip()
    name = str(p.get("product_name") or "").strip()
    sku = str(p.get("sku_code") or "").strip()
    return f"{brand + ' · ' if brand else ''}{name}{' [' + sku + ']' if sku else ''}"


def _guess_product_id(item: Dict[str, Any], products: List[Dict[str, Any]]) -> str:
    text = " ".join(
        str(item.get(k) or "")
        for k in ("product_family", "product_name", "description", "item_code")
    ).lower()
    if not text:
        return ""
    best_id, best_score = "", 0.0
    for p in products:
        hay = _label(p).lower()
        score = difflib.SequenceMatcher(None, text, hay).ratio()
        # Strong bonus for exact family/name containment.
        pname = str(p.get("product_name") or "").lower()
        if pname and (pname in text or text in pname):
            score += 0.35
        if score > best_score:
            best_score = score
            best_id = str(p.get("id") or "")
    return best_id if best_score >= 0.32 else ""


def _normalise_items(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in parsed.get("items") or []:
        product_name = item.get("product_name") or item.get("product_family") or item.get("description") or "Scanned item"
        qty = item.get("qty_pcs") or item.get("qty") or 1
        unit_price = item.get("unit_price_per_pc") or item.get("unit_price") or item.get("rate") or 0
        power = {
            "sph": item.get("sph"),
            "cyl": item.get("cyl"),
            "axis": item.get("axis"),
            "add": item.get("add"),
            "right": item.get("right") or {},
            "left": item.get("left") or {},
            "bc": item.get("bc"),
            "dia": item.get("dia"),
        }
        out.append({
            "description": item.get("description") or product_name,
            "product_name": product_name,
            "qty": qty,
            "unit_price": unit_price,
            "batch_no": item.get("batch_no") or "",
            "expiry_date": item.get("expiry_date") or "",
            "power_json": power,
            "raw": item,
        })
    return out


def render_invoice_scan_review_panel(
    invoice_path: str,
    *,
    supplier_id: str = "",
    supplier_name: str = "",
    key_prefix: str = "invoice_scan",
) -> None:
    """Render editable scan review and save confirmed rows."""
    if not invoice_path:
        return
    try:
        parsed = parse_invoice_file(invoice_path)
    except Exception as exc:
        st.error(f"Invoice scan failed: {exc}")
        return

    header = parsed.get("header") or {}
    totals = parsed.get("totals") or {}
    items = _normalise_items(parsed)
    products = _load_product_options()
    product_ids = [""] + [str(p["id"]) for p in products]
    product_by_id = {str(p["id"]): p for p in products}
    labels = {"": "— Select product —"}
    labels.update({str(p["id"]): _label(p) for p in products})

    st.markdown("#### 🧾 Scan Review")
    h1, h2, h3 = st.columns([2, 2, 2])
    doc_no = h1.text_input(
        "Invoice / Challan No",
        value=str(header.get("invoice_no") or ""),
        key=f"{key_prefix}_doc_no",
    )
    doc_date = h2.text_input(
        "Invoice Date",
        value=str(header.get("invoice_date") or header.get("delivery_date") or ""),
        key=f"{key_prefix}_doc_date",
    )
    doc_type = h3.selectbox("Type", ["INVOICE", "CHALLAN", "BOTH"], key=f"{key_prefix}_doc_type")

    if parsed.get("ocr", {}).get("ok") is False:
        st.warning(parsed.get("ocr", {}).get("error") or "OCR required")
    if parsed.get("preprocessed"):
        with st.expander("🖼 OCR Image Preview", expanded=False):
            img_path = parsed["preprocessed"].get("gray") or parsed["preprocessed"].get("rotated")
            if img_path:
                st.image(img_path, use_container_width=True)

    st.caption(
        f"Parsed {len(items)} line(s)"
        + (f" · Total ₹{_num(totals.get('total_amount')):,.2f}" if totals.get("total_amount") else "")
    )

    confirmed_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        guess = _guess_product_id(item, products)
        row_key = f"{key_prefix}_{idx}"
        with st.container(border=True):
            st.markdown(f"**{idx + 1}. {item['description']}**")
            # ── Brand filter → narrows product dropdown ───────────────────
            all_brands = sorted(set(p.get("brand","") for p in products if p.get("brand")))
            # Auto-detect brand from guess
            _guessed_brand = product_by_id.get(guess, {}).get("brand","") if guess else ""
            b1, b2 = st.columns([2, 3.2])
            _sel_brand = b1.selectbox(
                "Brand",
                ["All"] + all_brands,
                index=(["All"] + all_brands).index(_guessed_brand)
                       if _guessed_brand in all_brands else 0,
                key=f"{row_key}_brand",
                label_visibility="collapsed",
            )
            # Filter product list by brand
            _filtered_prods = [p for p in products
                                if _sel_brand == "All" or p.get("brand","") == _sel_brand]
            _filt_ids  = [""] + [str(p["id"]) for p in _filtered_prods]
            _filt_lbls = {"": "— Select product —"}
            _filt_lbls.update({str(p["id"]): _label(p) for p in _filtered_prods})
            # Re-check guess is in filtered list
            _fguess = guess if guess in _filt_ids else ""
            c1, c2 = st.columns([4, 1.2])
            selected = b2.selectbox(
                "Product",
                _filt_ids,
                index=_filt_ids.index(_fguess) if _fguess in _filt_ids else 0,
                format_func=lambda x: _filt_lbls.get(x, x),
                key=f"{row_key}_product",
            )
            chosen = product_by_id.get(selected, {})
            db_rate = _num(chosen.get("purchase_rate")) if chosen else 0.0
            rate_key = f"{row_key}_rate"
            product_state_key = f"{row_key}_rate_product"
            use_db_rate = c2.button("Use DB Rate", key=f"{row_key}_db_rate", use_container_width=True)
            item_rate = _num(item.get("unit_price"))
            if (
                product_state_key not in st.session_state
                or st.session_state.get(product_state_key) != selected
                or use_db_rate
            ):
                st.session_state[product_state_key] = selected
                if db_rate and (use_db_rate or not item_rate):
                    st.session_state[rate_key] = float(db_rate)
                elif rate_key not in st.session_state:
                    st.session_state[rate_key] = float(item_rate or 0)

            d1, d2, d3, d4 = st.columns([1, 1, 1.4, 1.4])
            qty = d1.number_input("Qty", min_value=0.0, value=_num(item.get("qty"), 1), step=1.0, key=f"{row_key}_qty")
            rate = d2.number_input("Purchase ₹", min_value=0.0, step=0.5, format="%.2f", key=rate_key)
            batch = d3.text_input("Batch", value=str(item.get("batch_no") or ""), key=f"{row_key}_batch")
            expiry = d4.text_input("Expiry", value=str(item.get("expiry_date") or ""), key=f"{row_key}_expiry")

            with st.expander("Power / Raw Details", expanded=False):
                st.json(item.get("power_json") or item.get("raw") or {})

            if selected:
                confirmed_items.append({
                    "product_id": selected,
                    "product_name": chosen.get("product_name") or item.get("product_name"),
                    "description": item.get("description"),
                    "qty": qty,
                    "unit_price": rate,
                    "batch_no": batch,
                    "expiry_date": expiry,
                    "power_json": item.get("power_json") or {},
                    "match_score": 100 if selected == guess else 75,
                    "raw": item.get("raw") or {},
                })

    total = sum(_num(x["qty"]) * _num(x["unit_price"]) for x in confirmed_items)
    st.markdown(f"**Confirmed total: ₹{total:,.2f}**")

    if st.button(
        "✅ Confirm Scanned Invoice & Save",
        type="primary",
        use_container_width=True,
        key=f"{key_prefix}_save",
        disabled=not confirmed_items,
    ):
        try:
            pno = record_scanned_invoice_items(
                supplier_id=supplier_id or "",
                supplier_name=supplier_name or str(header.get("supplier") or ""),
                document_no=doc_no,
                document_type=doc_type,
                document_date=_normalise_date(doc_date),
                invoice_file_path=invoice_path,
                items=confirmed_items,
            )
            st.success(f"Saved scanned invoice to procurement: {pno}")
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")

