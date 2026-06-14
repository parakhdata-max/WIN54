"""
modules/backoffice/_diag_pa_identity.py
══════════════════════════════════════════════════════════════════════════════
READ-ONLY diagnostic. Touches nothing. Run it to turn "nothing changed" from a
guess into a fact. It answers exactly three questions:

  1. Did migration 0009 actually apply?
  2. Do the 6 new columns exist on purchase_acknowledgements?
  3. For a given order_no, what is ACTUALLY stored in the new columns?

HOW TO RUN (pick one):

  A) Add a temporary page / button that calls render_pa_identity_diag(), e.g.
     in any admin screen:
         from modules.backoffice._diag_pa_identity import render_pa_identity_diag
         render_pa_identity_diag()

  B) Or from a Python shell with the app's DB env loaded:
         from modules.backoffice._diag_pa_identity import diag_text
         print(diag_text("R/2627/0121"))

This file is additive and safe to delete afterwards. It performs SELECT-only
queries — no INSERT/UPDATE/ALTER anywhere.
"""

from __future__ import annotations

_NEW_COLS = [
    "supplier_product_name",
    "supplier_product_code",
    "supplier_product_description",
    "our_product_name",
    "our_product_id",
    "mapping_source",
]


def _q(sql, params=None):
    from modules.sql_adapter import run_query
    return run_query(sql, params or {})


def diag_text(order_no: str | None = None) -> str:
    out = []

    # ── 1. migration applied? ────────────────────────────────────────────
    try:
        rows = _q(
            "SELECT version, applied_at FROM schema_migrations "
            "WHERE version = %(v)s",
            {"v": "0009_pa_supplier_product_identity"},
        )
        if rows:
            out.append(
                f"[1] migration 0009: APPLIED at {rows[0].get('applied_at')}"
            )
        else:
            out.append(
                "[1] migration 0009: NOT APPLIED — schema_migrations has no "
                "row for it. The app has not run the migration yet (deploy "
                "the .sql file and restart so run_pending_migrations() picks "
                "it up)."
            )
    except Exception as e:
        out.append(f"[1] migration check FAILED: {e!r} "
                   "(schema_migrations table missing? runner never ran?)")

    # ── 2. columns exist? ────────────────────────────────────────────────
    try:
        rows = _q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'purchase_acknowledgements'"
        )
        have = {str(r.get("column_name")) for r in (rows or [])}
        present = [c for c in _NEW_COLS if c in have]
        missing = [c for c in _NEW_COLS if c not in have]
        if not missing:
            out.append(f"[2] columns: ALL 6 PRESENT {present}")
        else:
            out.append(
                f"[2] columns: MISSING {missing} | present {present} — "
                "if missing, the migration did not run; saving procurement "
                "will ERROR (not silently do nothing)."
            )
    except Exception as e:
        out.append(f"[2] column check FAILED: {e!r}")

    # ── 3. what is actually stored for this order? ───────────────────────
    if order_no:
        try:
            rows = _q(
                """
                SELECT order_no, order_line_id,
                       supplier_name,
                       supplier_product_name,
                       supplier_product_description,
                       our_product_name, our_product_id,
                       mapping_source,
                       purchase_price, total_value,
                       billing_status, acknowledged_at
                FROM purchase_acknowledgements
                WHERE order_no = %(o)s
                ORDER BY acknowledged_at DESC NULLS LAST
                """,
                {"o": order_no},
            )
            if not rows:
                out.append(
                    f"[3] {order_no}: NO purchase_acknowledgements rows. "
                    "Either procurement was never saved for this order, OR "
                    "it was saved before the patch — in which case the new "
                    "columns are NULL for older rows (expected, not a bug)."
                )
            else:
                out.append(f"[3] {order_no}: {len(rows)} PA row(s):")
                for r in rows:
                    out.append(
                        "    • line "
                        f"{str(r.get('order_line_id'))[:8]} | "
                        f"sup='{r.get('supplier_product_name')}' | "
                        f"our='{r.get('our_product_name')}' | "
                        f"src={r.get('mapping_source')} | "
                        f"price={r.get('purchase_price')} | "
                        f"status={r.get('billing_status')}"
                    )
        except Exception as e:
            out.append(f"[3] order read FAILED: {e!r}")

    return "\n".join(out)


def render_pa_identity_diag():
    """Streamlit panel wrapper (read-only)."""
    import streamlit as st
    st.subheader("PA Supplier-Identity Diagnostic (read-only)")
    _ono = st.text_input("Order No to inspect", value="R/2627/0121")
    if st.button("Run diagnostic"):
        st.code(diag_text(_ono.strip() or None), language="text")
