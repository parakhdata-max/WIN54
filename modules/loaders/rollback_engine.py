"""
modules/loaders/rollback_engine.py
====================================
Import Rollback Engine — DV ERP

Allows controlled undo of a previous LIVE import using:
  - loader_import_log    (find the import to undo)
  - loader_row_history   (identify affected row hashes)

PHILOSOPHY — Soft Rollback Only:
  ✔ Shows which rows were affected
  ✔ Confirms hash count before any action
  ✔ Deletes row hash history (marks rows as "unseen" so they can be re-imported)
  ✔ Shows SQL for manual entity delete (for operator to verify/run)
  ✗ Does NOT blindly delete from entity tables (too risky without entity mapping)

Why this is safe:
  Row hashes are the dedup guard. Deleting a hash means:
    → The same row can be imported again (re-import is possible)
    → But it does NOT automatically undo the DB entity record
  Full entity rollback requires mapping hashes → entity IDs per file_type.
  That mapping is shown to the operator as SQL they can verify and run.

ENTITY TABLE MAP (extend as your schema evolves):
  PRODUCT  → products          (WHERE product_code IN (...))
  FRAME    → frame_stock       (WHERE sku_code IN (...))
  PARTY    → parties           (WHERE party_name IN (...))
  PATIENT  → patient_visits    (WHERE id IN (...))
  OPHLENS  → inventory_stock   (WHERE import_id = <uuid>)
  CLENS    → inventory_stock
  SOL      → inventory_stock
  BLANK    → blank_inventory
"""

import json
import streamlit as st


# ── Helpers ───────────────────────────────────────────────────────────────────

def _q(sql: str, params=None):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception as e:
        st.error(f"Query error: {e}")
        return []


def _w(sql: str, params=None) -> bool:
    try:
        from modules.sql_adapter import run_write
        run_write(sql, params)
        return True
    except Exception as e:
        st.error(f"Write error: {e}")
        return False


# ── Entity rollback SQL generator ─────────────────────────────────────────────
# Returns a guidance SQL string shown to operator — NOT auto-executed.

_ENTITY_ROLLBACK_SQL = {
    "PRODUCT": "DELETE FROM products WHERE import_id = '{import_id}';",
    "FRAME":   "DELETE FROM frame_stock WHERE import_id = '{import_id}';",
    "PARTY":   "DELETE FROM parties WHERE import_id = '{import_id}';",
    "PATIENT": "DELETE FROM patient_visits WHERE import_id = '{import_id}';",
    "OPHLENS": "DELETE FROM inventory_stock WHERE import_id = '{import_id}';",
    "CLENS":   "DELETE FROM inventory_stock WHERE import_id = '{import_id}';",
    "SOL":     "DELETE FROM inventory_stock WHERE import_id = '{import_id}';",
    "BLANK":   "DELETE FROM blank_inventory WHERE import_id = '{import_id}';",
}

# Note: The above SQL works only if your entity tables have an import_id column.
# If not yet present, the operator must match by other keys (hash-based approach shown below).


# ══════════════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════

def render_rollback_ui():
    st.title("🔁 Import Rollback")
    st.caption("Controlled undo of a previous LIVE import. Review carefully before confirming.")

    # ── Safety banner ─────────────────────────────────────────────────────────
    st.warning(
        "⚠️ **Rollback Philosophy:** This tool removes row hashes (allowing re-import) "
        "and shows you the SQL to delete entity records. "
        "Entity deletion is shown for your review — not auto-executed. "
        "Always verify in DRY mode first."
    )

    st.divider()

    # ── Load recent LIVE/SHADOW imports ───────────────────────────────────────
    imports = _q("""
        SELECT
            import_id,
            file_name,
            file_type,
            mode,
            stock_mode,
            status,
            rows_total,
            rows_ok,
            "user",
            imported_at
        FROM loader_import_log
        WHERE status IN ('OK', 'PARTIAL', 'SHADOW')
        ORDER BY imported_at DESC
        LIMIT 50
    """)

    if not imports:
        st.info("No LIVE or SHADOW imports found to rollback.")
        return

    # ── Import selector ───────────────────────────────────────────────────────
    st.subheader("1. Select Import to Rollback")

    def _label(x):
        at  = str(x.get("imported_at", ""))[:16]
        ft  = x.get("file_type", "")
        fn  = x.get("file_name", "")
        ok  = x.get("rows_ok", 0)
        usr = x.get("user", "")
        return f"{at}  |  {ft}  |  {fn}  |  {ok} rows  |  by {usr}"

    selected = st.selectbox(
        "Choose import",
        imports,
        format_func=_label,
    )

    if not selected:
        return

    import_id = str(selected.get("import_id", ""))
    file_type = selected.get("file_type", "")
    file_name = selected.get("file_name", "")
    rows_ok   = selected.get("rows_ok", 0)
    mode      = selected.get("mode", "")
    at        = str(selected.get("imported_at", ""))[:19]

    # ── Import detail ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("2. Import Details")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("File Type",   file_type)
    c2.metric("Mode",        mode)
    c3.metric("Rows OK",     rows_ok)
    c4.metric("Imported At", at)

    st.code(f"Import ID: {import_id}", language="text")

    # ── Load affected hashes ──────────────────────────────────────────────────
    st.divider()
    st.subheader("3. Affected Row Hashes")

    hashes = _q("""
        SELECT row_hash, imported_at
        FROM loader_row_history
        WHERE import_id = %s
        ORDER BY imported_at
    """, (import_id,))

    hash_count = len(hashes)

    if hash_count == 0:
        st.info(
            "No row hashes found for this import. "
            "Either hashes were never stored (DRY/old import) or already rolled back."
        )
    else:
        st.metric("Row Hashes to Remove", hash_count)
        st.caption(
            f"Removing these {hash_count} hashes will allow the same rows to be re-imported. "
            "Entity records in DB are NOT automatically deleted."
        )

        with st.expander(f"Preview row hashes ({min(hash_count, 10)} of {hash_count})"):
            for h in hashes[:10]:
                st.code(h.get("row_hash", ""), language="text")
            if hash_count > 10:
                st.caption(f"… and {hash_count - 10} more")

    # ── Entity rollback SQL ───────────────────────────────────────────────────
    st.divider()
    st.subheader("4. Entity Delete SQL (Review Before Running)")

    sql_template = _ENTITY_ROLLBACK_SQL.get(
        file_type,
        f"-- No auto-SQL for file_type '{file_type}'. Manually query entity table."
    )
    entity_sql = sql_template.format(import_id=import_id)

    st.code(entity_sql, language="sql")
    st.caption(
        "⚠️ Run this SQL in your DB client AFTER verifying the import ID is correct. "
        "This tool does NOT execute entity deletes automatically."
    )

    # ── Rollback confirmation ─────────────────────────────────────────────────
    st.divider()
    st.subheader("5. Confirm Rollback")

    if hash_count == 0:
        st.info("Nothing to rollback — no hashes stored for this import.")
        return

    confirm = st.checkbox(
        f"I understand this will remove {hash_count} row hashes and mark this import as ROLLED_BACK"
    )

    if not confirm:
        st.caption("Check the box above to enable rollback.")
        return

    col_btn, col_cancel = st.columns([1, 3])
    with col_btn:
        do_rollback = st.button(
            "🔁 Execute Hash Rollback",
            type="primary",
            use_container_width=True,
        )

    if do_rollback:
        with st.spinner("Removing row hashes..."):

            # Step 1: Delete row hashes
            ok1 = _w("""
                DELETE FROM loader_row_history
                WHERE import_id = %s
            """, (import_id,))

            # Step 2: Mark import as rolled back in log
            ok2 = _w("""
                UPDATE loader_import_log
                SET status = 'ROLLED_BACK'
                WHERE import_id = %s
            """, (import_id,))

        if ok1 and ok2:
            st.success(
                f"✅ Rollback complete — {hash_count} row hashes removed. "
                f"Import `{import_id[:8]}…` marked as ROLLED_BACK."
            )
            st.info(
                "**Next steps:**\n"
                f"1. Run the entity delete SQL above in your DB client\n"
                f"2. Re-import `{file_name}` (corrected) — dedup protection is now cleared\n"
                f"3. Verify data in Audit & Integrity tab"
            )
        else:
            st.error("Rollback partially failed. Check DB connection and try again.")

    # ── Rollback history ──────────────────────────────────────────────────────
    st.divider()
    with st.expander("📋 Rollback History"):
        rolled = _q("""
            SELECT import_id, file_name, file_type, "user", imported_at
            FROM loader_import_log
            WHERE status = 'ROLLED_BACK'
            ORDER BY imported_at DESC
            LIMIT 20
        """)
        if rolled:
            import pandas as pd
            st.dataframe(
                pd.DataFrame([{
                    "Import ID":   str(r.get("import_id", ""))[:8] + "…",
                    "File":        r.get("file_name", ""),
                    "Type":        r.get("file_type", ""),
                    "Operator":    r.get("user", ""),
                    "At":          str(r.get("imported_at", ""))[:16],
                } for r in rolled]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No rollbacks recorded yet.")
