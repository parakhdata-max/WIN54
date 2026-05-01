"""
modules/printing/label_print_ui.py
=====================================
Reusable Streamlit print widget.
Drop render_print_button() anywhere to add print capability.
"""
import streamlit as st


def _get_shop_name() -> str:
    try:
        from modules.sql_adapter import run_query
        row = run_query("SELECT value FROM system_flags WHERE key='shop_name' LIMIT 1") or []
        if row: return str(row[0].get("value","DV Optical"))
    except: pass
    return st.session_state.get("shop_name_override", "DV Optical")


def render_print_button(
    code: str,
    price,
    label: str = "🖨️ Print Label",
    key_suffix: str = "",
    shop: str = None,
    compact: bool = False,
):
    """
    Single label print button.
    code   : barcode value (SKU / batch_no / PAT number)
    price  : MRP value — number or string
    compact: if True, uses smaller inline layout
    """
    shop = shop or _get_shop_name()

    if compact:
        if st.button(label, key=f"print_btn_{key_suffix}_{code}", use_container_width=True):
            _do_print_single(code, shop, price, key_suffix)
        return

    with st.expander(f"🖨️ Print label for {code}", expanded=False):
        c1, c2, c3 = st.columns([2, 1, 1])
        shop_input  = c1.text_input("Shop name on label", value=shop,
                                    key=f"print_shop_{key_suffix}_{code}")
        price_input = c2.text_input("Price", value=str(price),
                                    key=f"print_price_{key_suffix}_{code}")
        copies      = c3.number_input("Copies", min_value=1, value=1,
                                      key=f"print_copies_{key_suffix}_{code}")

        col_btn, col_prev = st.columns([1, 1])
        with col_btn:
            if st.button("🖨️ Print Now", type="primary",
                         key=f"print_go_{key_suffix}_{code}", use_container_width=True):
                _do_print_single(code, shop_input, price_input, key_suffix, copies)
        with col_prev:
            if st.button("👁️ Preview TSPL", key=f"print_prev_{key_suffix}_{code}",
                         use_container_width=True):
                from modules.printing.label_printer import get_tspl_preview
                st.code(get_tspl_preview(code, shop_input, price_input), language="text")


def _do_print_single(code, shop, price, key_suffix, copies=1):
    try:
        from modules.printing.label_printer import print_label
        ok, msg = print_label(str(code), str(shop), price, copies)
        if ok:
            st.success(f"✅ Printed {copies}× label for **{code}**")
        else:
            st.error(f"❌ Print failed: {msg}")
    except Exception as ex:
        st.error(f"❌ Printer error: {ex}")


def render_batch_print_ui(items: list, key: str = "batch", shop: str = None):
    """
    Batch print UI for a list of items.
    items = list of dicts with keys: code/batch_no, mrp/price, product_name (optional)
    Shows a table with checkboxes, copies input, and Print Selected button.
    """
    if not items:
        return

    shop = shop or _get_shop_name()

    st.markdown("#### 🖨️ Print Labels")
    c1, c2, c3 = st.columns([3, 1, 1])
    c1.caption("Select items to print")
    shop_input = c2.text_input("Shop name", value=shop, key=f"bp_shop_{key}",
                               label_visibility="collapsed")
    default_copies = c3.number_input("Default copies", min_value=1, value=1,
                                     key=f"bp_copies_{key}", label_visibility="collapsed")

    # Selection table
    selected = []
    for i, item in enumerate(items):
        code  = str(item.get("batch_no") or item.get("code") or item.get("sku","")).strip()
        price = item.get("mrp") or item.get("price") or 0
        name  = item.get("product_name","")
        loc   = item.get("location","")

        col_chk, col_info, col_copies = st.columns([0.5, 4, 1])
        checked = col_chk.checkbox("", value=True, key=f"bp_chk_{key}_{i}",
                                   label_visibility="collapsed")
        col_info.markdown(
            f"**{code}** · {name}" +
            (f" · 📍{loc}" if loc else "") +
            (f" · ₹{float(price):.0f}" if price else "")
        )
        copies = col_copies.number_input("", min_value=1, value=int(default_copies),
                                         key=f"bp_n_{key}_{i}", label_visibility="collapsed")
        if checked:
            selected.append({"code": code, "shop": shop_input,
                             "price": price, "qty": copies})

    st.caption(f"{len(selected)} of {len(items)} selected")

    if selected and st.button(f"🖨️ Print {len(selected)} label(s)",
                               type="primary", key=f"bp_go_{key}", use_container_width=True):
        from modules.printing.label_printer import print_batch
        result = print_batch(selected, shop=shop_input)
        if result["printed"]:
            st.success(f"✅ Printed {result['printed']} label(s)")
        if result["failed"]:
            st.error(f"❌ {result['failed']} failed: {'; '.join(result['errors'][:3])}")
