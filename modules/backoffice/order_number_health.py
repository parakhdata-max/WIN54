"""
modules/backoffice/order_number_health.py
==========================================
Order Number Health — Admin Panel

Shows:
  - Current counter state per series
  - Gap audit (which numbers are missing and why)
  - Consultation vs Retail series separation status
  - One-click repair: stamp missing display_order_no on orphaned rows
  - Live concurrent-save monitor (shows registry lock contention)
"""

from __future__ import annotations
import streamlit as st


def render_order_number_health():
    from modules.security.roles import require_role
    require_role("admin", "manager")

    st.markdown("## 🔢 Order Number Health")
    st.caption(
        "Monitors sequential order numbering across all servers. "
        "Uses transactional row-locks — gaps indicate pre-registry orders or manual DB edits."
    )

    # ── Registry status ───────────────────────────────────────────────────────
    st.markdown("### Counter Registry")
    try:
        from modules.db.order_number_registry import registry_status
        rows = registry_status()
        if rows:
            _cols = st.columns([2, 1.5, 1.5, 2, 2])
            for h, c in zip(["Series", "Last Number", "Prefix", "Fiscal Year", "Updated"], _cols):
                c.markdown(f"<div style='font-size:0.72rem;color:#6b7280;font-weight:700'>{h}</div>",
                           unsafe_allow_html=True)
            for r in rows:
                c1, c2, c3, c4, c5 = st.columns([2, 1.5, 1.5, 2, 2])
                c1.markdown(f"**{r['series']}**")
                c2.markdown(f"`{r['last_number']}`")
                c3.markdown(r.get("prefix", "—"))
                c4.markdown(r.get("fiscal_year", "—"))
                c5.markdown(str(r.get("updated_at", ""))[:16])
        else:
            st.info("Registry table not yet created — will be created on next order save.")
    except Exception as e:
        st.error(f"Registry read failed: {e}")

    st.markdown("---")

    # ── Gap audit ─────────────────────────────────────────────────────────────
    st.markdown("### Gap Audit")

    _series_opts = ["RETAIL (all non-consultation)", "CONSULTATION", "PURCHASE"]
    _sel = st.radio("Audit series", _series_opts, horizontal=True, key="gap_audit_series")
    _ot_map = {
        "RETAIL (all non-consultation)": "RETAIL",
        "CONSULTATION": "CONSULTATION",
        "PURCHASE": "PURCHASE",
    }
    _ot = _ot_map[_sel]

    if st.button("🔍 Run Audit", key="run_gap_audit", type="primary"):
        with st.spinner("Scanning orders..."):
            from modules.db.order_number_registry import audit_gaps
            result = audit_gaps(_ot)

        if "error" in result:
            st.error(f"Audit failed: {result['error']}")
        else:
            _gap_count = result.get("gap_count", 0)
            _miss = result.get("missing_count", 0)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Orders", result.get("total_orders", 0))
            m2.metric("First #", result.get("min_no", "—"))
            m3.metric("Last #", result.get("max_no", "—"))
            m4.metric("Missing Numbers", _miss,
                      delta=f"{_gap_count} gap{'s' if _gap_count!=1 else ''}",
                      delta_color="inverse")

            if _gap_count == 0:
                st.success("✅ Perfect — no gaps found. All numbers are sequential.")
            else:
                st.warning(f"⚠️ {_gap_count} gap{'s' if _gap_count!=1 else ''} — "
                           f"{_miss} number{'s' if _miss!=1 else ''} missing.")

                # Show gap table
                st.markdown("**Gap detail:**")
                for from_no, to_no in result["gaps"]:
                    missing = list(range(from_no + 1, to_no))
                    st.markdown(
                        f"<div style='background:#1a0a0a;border-left:3px solid #ef4444;"
                        f"padding:6px 12px;border-radius:4px;margin:3px 0;"
                        f"font-family:monospace;font-size:0.8rem'>"
                        f"Gap after <b>{from_no}</b> → next is <b>{to_no}</b> &nbsp;·&nbsp; "
                        f"missing: {', '.join(str(n) for n in missing[:10])}"
                        f"{'...' if len(missing) > 10 else ''}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                # Explain likely causes
                with st.expander("Why do gaps occur?"):
                    st.markdown("""
**Pre-registry orders (old PostgreSQL SEQUENCE):**
Numbers 1–207 were assigned by `orders_display_seq`. PostgreSQL sequences
advance even on transaction rollback, so any failed or abandoned save consumed
a number permanently.

**After deploying this registry:**
All new saves use `SELECT ... FOR UPDATE` on the registry row. The counter only
advances when the transaction commits. Rollback = counter rollback = zero gap.

**Consultations on the retail sequence (now fixed):**
Old code gave consultations a display_order_no from the retail sequence.
Consultations are now on a separate `CONSULTATION` series — they no longer
consume retail numbers.

**What to do about existing gaps:**
Existing gaps (pre-registry) are historical. You can note them in the audit
but there is nothing to fill them with — the orders were never created.
Going forward: zero new gaps.
                    """)

    st.markdown("---")

    # ── Consultation separation check ─────────────────────────────────────────
    st.markdown("### Consultation Separation Check")
    st.caption("Consultations should have their own series and NOT appear in the retail sequence.")

    if st.button("🔍 Check Consultations", key="check_consult"):
        try:
            from modules.sql_adapter import run_query
            # Find consultations that have display_order_no
            cons = run_query("""
                SELECT order_no, display_order_no, created_at
                FROM orders
                WHERE order_type = 'CONSULTATION'
                  AND display_order_no IS NOT NULL
                  AND display_order_no > 0
                ORDER BY display_order_no DESC
                LIMIT 20
            """) or []

            if not cons:
                st.success("✅ No consultations found with display_order_no — series is clean.")
            else:
                st.info(f"ℹ️ {len(cons)} consultations have display_order_no values "
                        f"(these are on the CONSULTATION series now).")
                # Check if any are on the RETAIL sequence (would cause gaps)
                max_cons = max(r["display_order_no"] for r in cons)
                max_retail = (run_query("""
                    SELECT COALESCE(MAX(display_order_no), 0) AS m FROM orders
                    WHERE order_type NOT IN ('CONSULTATION','PURCHASE')
                      AND display_order_no IS NOT NULL
                """) or [{"m": 0}])[0]["m"]

                if max_cons > max_retail:
                    st.warning(
                        f"⚠️ Highest consultation number ({max_cons}) > "
                        f"highest retail number ({max_retail}). "
                        "Consultations may have consumed retail sequence numbers historically."
                    )
                else:
                    st.success("✅ Consultation numbers are within their own range.")

        except Exception as e:
            st.error(f"Check failed: {e}")

    st.markdown("---")

    # ── Repair: stamp NULL display_order_no ───────────────────────────────────
    st.markdown("### Repair — Stamp Missing Numbers")
    st.caption(
        "Orders saved before this registry was deployed may have NULL display_order_no. "
        "This stamps them with sequential values in creation order, preserving existing numbers."
    )

    if st.button("🔍 Find Orders with NULL display_order_no", key="find_null"):
        try:
            from modules.sql_adapter import run_query
            null_rows = run_query("""
                SELECT COUNT(*) AS cnt FROM orders
                WHERE display_order_no IS NULL
                  AND COALESCE(is_deleted, FALSE) = FALSE
            """) or [{"cnt": 0}]
            cnt = null_rows[0]["cnt"]
            if cnt == 0:
                st.success("✅ All orders have display_order_no — nothing to repair.")
            else:
                st.warning(f"⚠️ {cnt} orders missing display_order_no.")
                st.session_state["_null_display_count"] = cnt
        except Exception as e:
            st.error(f"Check failed: {e}")

    if st.session_state.get("_null_display_count", 0) > 0:
        st.warning(
            f"**Confirm:** Stamp {st.session_state['_null_display_count']} orders with "
            "sequential display numbers (in creation order)? This cannot be undone."
        )
        if st.button("✅ Stamp Missing Numbers", key="stamp_null", type="primary"):
            try:
                from modules.sql_adapter import run_write, run_query
                # Get orders with NULL display_order_no, sorted by created_at
                null_orders = run_query("""
                    SELECT id, order_type, created_at
                    FROM orders
                    WHERE display_order_no IS NULL
                      AND COALESCE(is_deleted, FALSE) = FALSE
                    ORDER BY created_at ASC
                """) or []

                # Determine current max per series
                max_retail = (run_query("""
                    SELECT COALESCE(MAX(display_order_no), 0) AS m FROM orders
                    WHERE order_type NOT IN ('CONSULTATION','PURCHASE')
                      AND display_order_no IS NOT NULL
                """) or [{"m": 0}])[0]["m"]
                max_cons = (run_query("""
                    SELECT COALESCE(MAX(display_order_no), 0) AS m FROM orders
                    WHERE order_type = 'CONSULTATION'
                      AND display_order_no IS NOT NULL
                """) or [{"m": 0}])[0]["m"]

                retail_ctr = max_retail
                cons_ctr   = max_cons
                stamped    = 0

                for row in null_orders:
                    ot = str(row.get("order_type") or "RETAIL").upper()
                    if ot == "CONSULTATION":
                        cons_ctr += 1
                        new_no = cons_ctr
                    else:
                        retail_ctr += 1
                        new_no = retail_ctr
                    run_write(
                        "UPDATE orders SET display_order_no = %(n)s WHERE id = %(id)s::uuid",
                        {"n": new_no, "id": str(row["id"])}
                    )
                    stamped += 1

                # Sync registry to new max
                run_write("""
                    UPDATE order_number_registry
                    SET last_number = GREATEST(last_number, %(m)s), updated_at = NOW()
                    WHERE series = 'RETAIL'
                """, {"m": retail_ctr})
                run_write("""
                    UPDATE order_number_registry
                    SET last_number = GREATEST(last_number, %(m)s), updated_at = NOW()
                    WHERE series = 'CONSULTATION'
                """, {"m": cons_ctr})

                st.success(f"✅ Stamped {stamped} orders. Retail max: {retail_ctr}, Consultation max: {cons_ctr}.")
                st.session_state.pop("_null_display_count", None)
            except Exception as e:
                st.error(f"Stamp failed: {e}")

    st.markdown("---")

    # ── How the lock works ────────────────────────────────────────────────────
    # ── All series status ────────────────────────────────────────────────────
    st.markdown("### All Document Series")
    st.caption("Every document type — one registry, one source of truth.")

    _ALL_SERIES = [
        ("RETAIL",           "Sales Orders",       "R/2526/0042"),
        ("CONSULTATION",     "Consultations",      "CONS/2526/0012"),
        ("CHALLAN",          "Delivery Challans",  "CH/2526/0042"),
        ("INVOICE",          "Sales Invoices",     "INV/2526/0042"),
        ("CREDIT_NOTE",      "Credit Notes",       "CN/2526/0007"),
        ("DEBIT_NOTE",       "Debit Notes",        "DN/2526/0003"),
        ("PAYMENT",          "Payments",           "PAY/2526/0018"),
        ("PURCHASE_ORDER",   "Purchase Orders",    "PO/2526/0009"),
        ("PURCHASE_INVOICE", "Purchase Invoices",  "PINV/2526/0009"),
        ("JOURNAL",          "Journal Vouchers",   "JV/2526/0001"),
        ("RETURN",           "Returns",            "RET/2526/0002"),
    ]

    try:
        from modules.db.order_number_registry import registry_status, format_doc_number
        _reg = {r["series"]: r for r in registry_status()}

        _hc = st.columns([2.5, 1.5, 2, 2.5])
        for h, c in zip(["Document Type", "Last #", "Next Number", "Series"], _hc):
            c.markdown(f"<div style='font-size:0.7rem;color:#6b7280;font-weight:700'>{h}</div>",
                       unsafe_allow_html=True)

        for series, label, example in _ALL_SERIES:
            reg = _reg.get(series, {})
            last = int(reg.get("last_number") or 0)
            next_no = format_doc_number(series, last + 1) if last >= 0 else "—"
            _rc = st.columns([2.5, 1.5, 2, 2.5])
            _rc[0].markdown(f"**{label}**")
            _rc[1].markdown(f"`{last}`" if last else "<span style='color:#475569'>—</span>",
                            unsafe_allow_html=True)
            _rc[2].markdown(
                f"<span style='color:#6366f1;font-family:monospace'>{next_no}</span>",
                unsafe_allow_html=True
            )
            _rc[3].markdown(f"<span style='color:#475569;font-size:0.72rem'>{series}</span>",
                            unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Could not load series status: {e}")

    st.markdown("---")

    # ── Year-end reset ─────────────────────────────────────────────────────────
    st.markdown("### Fiscal Year Reset")
    st.caption(
        "At the start of each financial year (1st April), counters can be reset to 1. "
        "Old numbers remain in the database — only the counter resets. "
        "The fiscal year in the format (e.g. /2627/) changes automatically."
    )

    from modules.security.roles import has_role
    if not has_role("admin"):
        st.info("Admin access required for year-end reset.")
    else:
        _fy_cols = st.columns([3, 2])
        with _fy_cols[0]:
            _reset_series = st.multiselect(
                "Series to reset",
                [s for s, _, _ in _ALL_SERIES],
                default=[],
                key="fy_reset_series"
            )
        with _fy_cols[1]:
            _new_start = st.number_input(
                "Reset to start at", min_value=0, value=0, step=1,
                key="fy_reset_start",
                help="0 means next number will be 1"
            )

        _fy_step2 = "fy_reset_confirm"
        if _reset_series:
            if not st.session_state.get(_fy_step2):
                if st.button("🔄 Reset Selected Series", key="fy_reset_btn",
                             use_container_width=True):
                    st.session_state[_fy_step2] = True
                    st.rerun()
            else:
                st.warning(
                    f"Reset counters for: **{', '.join(_reset_series)}** to {_new_start}? "
                    "Documents created after reset will have new fiscal year in their numbers. "
                    "This CANNOT be undone."
                )
                _fr1, _fr2 = st.columns(2)
                with _fr1:
                    if st.button("✅ Confirm Reset", type="primary",
                                 key="fy_reset_yes", use_container_width=True):
                        try:
                            from modules.sql_adapter import run_write
                            from modules.db.order_number_registry import _current_fiscal_year
                            for s in _reset_series:
                                run_write("""
                                    UPDATE order_number_registry
                                    SET last_number = %(n)s,
                                        fiscal_year  = %(fy)s,
                                        updated_at   = NOW()
                                    WHERE series = %(s)s
                                """, {"n": _new_start, "fy": _current_fiscal_year(), "s": s})
                            st.success(f"✅ Reset {len(_reset_series)} series to {_new_start}.")
                            st.session_state.pop(_fy_step2, None)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Reset failed: {e}")
                with _fr2:
                    if st.button("← Cancel", key="fy_reset_no", use_container_width=True):
                        st.session_state.pop(_fy_step2, None)
                        st.rerun()

    st.markdown("---")

    with st.expander("ℹ️ Concurrency Architecture — How This Works", expanded=False):
        st.markdown("""
## How WIN54 handles multi-user concurrency (SAP / Tally pattern)

### 1. Document Numbers — Gap-Free Registry

| Old (PostgreSQL SEQUENCE) | New (Registry FOR UPDATE) |
|--------------------------|--------------------------|
| `nextval()` fires at INSERT execution | Lock acquired at INSERT execution |
| Rollback → number consumed → **gap** | Rollback → counter rollback → **zero gap** |
| Race between servers | DB serialises all servers through one row |
| Consultation uses retail sequence | Consultation has own series |

```
Server A + Server B hit Save simultaneously:

A: SELECT last_number=211 FOR UPDATE   ← A locks row
B: SELECT last_number FOR UPDATE       ← B WAITS here (DB blocks it)

A: UPDATE last_number=212
A: INSERT order(display_no=212)
A: COMMIT → lock released, 212 visible

B: (unblocked) SELECT last_number=212 FOR UPDATE
B: UPDATE last_number=213
B: INSERT order(display_no=213)
B: COMMIT → 213 visible

Result: 212, 213 — perfectly sequential, zero gap, even under 50 concurrent saves.
```

### 2. Status Changes — Optimistic Lock (SAP pattern)

Every status move uses `WHERE status = expected_status`:
```sql
UPDATE orders
SET    status = 'IN_PRODUCTION'
WHERE  order_no = 'R/2526/0212'
  AND  status   = 'CONFIRMED'    ← pre-condition
```
- If rows_affected = 1 → success, we moved it
- If rows_affected = 0 → someone else moved it first → user gets warning to refresh

### 3. Consultation Conversion — Atomic Guard

```sql
UPDATE orders
SET    is_converted = TRUE, linked_retail_no = 'R/2526/0212'
WHERE  order_no = 'CONS/2526/0042'
  AND  (is_converted IS NULL OR is_converted = FALSE)  ← only fires once
```
Only one of two simultaneous conversions can succeed. The other gets rows_affected=0.

### 4. Stock Allocation — FOR UPDATE on batch row

```sql
SELECT physical_qty, allocated_qty FROM inventory_stock
WHERE  product_id = X AND batch_no = Y
FOR UPDATE   ← exclusive row lock

-- Check available >= requested, then:
UPDATE inventory_stock SET allocated_qty = allocated_qty + N
```
Two staff can't allocate the same physical unit. One waits, checks, finds 0 available, fails cleanly.

### 5. Stale UI Detection

Before every write, the current DB status is fetched and compared to the UI state.
If another user changed the order since this page loaded → warning shown, write blocked.
User must refresh to proceed.

### 6. Document Number Format

All documents use fiscal-year-aware format:
```
PREFIX / FYYYYY / NNNN
CH / 2526 / 0042    ← Challan 42 of FY 2025-26
INV / 2526 / 0042   ← Invoice 42
CN / 2526 / 0007    ← Credit Note 7
JV / 2526 / 0001    ← Journal Voucher 1
```
FY changes automatically on 1st April. Manual reset available for year-end close.
        """)
