# WIN54 Focused Patch — Phase 1

Surgical fix for **6 issues**, all in the procurement data flow. No SQL changes,
no schema changes, no touches to punching/print code.

## Issues addressed

| # | Issue | File | Status |
|---|---|---|---|
| 1 | Supplier picker showing in Backoffice Shift UI | `assignment_panel.py` | **REMOVED** |
| 3 | R+L Save Ref didn't promote to SUPPLIER_CONFIRMED → lines never reached Procurement Queue | `supplier_pipeline.py` | **FIXED** |
| 4 | Stock Save Ref hidden inside collapsed expander → staff missed the next step | `stock_pipeline.py` | **FIXED** (auto-expand + amber banner) |
| 5 | "PO done but still selectable" | `stock_pipeline.py` | **NOT A BUG** — selection guard already in place at lines 554-557. Verified. |
| 6 | "Can see PO" tab under Stock | `stock_pipeline.py` | **ADDED** — 📄 Stock POs subtab with Resend / Cancel actions |
| 10 | External Supplier same issue as Supplier | `supplier_pipeline.py` | **COVERED** — same file handles both VENDOR and EXTERNAL_LAB |
| — | Procurement Queue PA exclusion too narrow (`NOT_READY` PAs leaked through) | `procurement_queue.py` | **FIXED** |

## Issues deferred to next round

| # | Why deferred |
|---|---|
| 2 (tab jumping) | Partial coverage only this round — added `_prod_lazy_panel_next` preservation to Save Ref hot paths (Stock and R+L Supplier). Comprehensive fix needs auditing every `st.rerun()` across production_page, which is a separate task. |
| 7 (Power save mandatory) | retail_punching.py is 8,500 lines. Not safe to touch without isolated read pass. |
| 8 (Print Order redirect) | Same — print/save flow needs its own focused round. |
| 9 (WhatsApp after save) | Same. |

## Files in this drop

| File | Path |
|---|---|
| `supplier_pipeline.py` | `modules/backoffice/supplier_pipeline.py` |
| `stock_pipeline.py` | `modules/backoffice/stock_pipeline.py` |
| `procurement_queue.py` | `modules/backoffice/procurement_queue.py` |
| `assignment_panel.py` | `modules/backoffice/assignment_panel.py` |

## Patch details

### Patch A — `supplier_pipeline.py` (R+L Save Ref promotion)

**Bug:** R+L combined "Save Ref" button saved `supplier_order_no` but didn't
flip `supplier_stage` from `ORDER_PLACED` to `SUPPLIER_CONFIRMED`. Lines were
stuck at the start of the pipeline forever and never appeared in Procurement
Queue.

**Fix:** Around line 1023-1052. After saving the ref, promote both R and L
lines to `SUPPLIER_CONFIRMED` (only if still at `ORDER_PLACED` — won't
downgrade later stages). Also preserves the current tab on rerun so staff
isn't bounced to Dashboard.

**Covers Issue #10:** Same code path handles `EXTERNAL_LAB` route.

### Patch B — `stock_pipeline.py` (Save Ref visibility)

**Bug:** When a stock line was at `PO_SENT`, the Save Ref input was inside a
collapsed `📋 Supplier confirmation` expander. Staff didn't see it and the
line never advanced to `ORDERED`.

**Fix:** Around line 989. Auto-expand the expander (`expanded=True`) for
`PO_SENT` lines. Added an amber banner above explaining "Awaiting supplier
confirmation — enter ref below → line moves to Procurement Queue." Save
preserves the current tab.

### Patch C — `procurement_queue.py` (PA filter simplification)

**Bug:** Queue's `NOT EXISTS purchase_acknowledgements` clause only excluded
PAs with `billing_status IN ('PROCURED','PURCHASE_ACKED','LOCKED','READY')`.
But `supplier_pipeline.py`'s "Save Purchase Invoice" writes PAs with
`billing_status='NOT_READY'`. Those lines kept appearing in the queue even
after invoice was already saved through the supplier-side path.

**Fix:** Around line 195. Simplified the exclusion to: any PA with
`purchase_price > 0` removes the line from the queue. Matches GPT's "blocker"
recommendation.

### Patch D — `assignment_panel.py` (remove supplier picker)

**Bug:** The "Shift route" UI in backoffice showed a supplier dropdown when
switching to VENDOR/EXTERNAL_LAB. This crossed responsibilities — backoffice
should only set the route; supplier picking belongs to the procurement team.

**Fix:** Around line 1929-1947. Removed the `_get_ranked_suppliers_for_product`
call and selectbox. Replaced with a caption directing staff to "Production →
Supplier" or "Production → External Supplier" depending on route. The
`_apply_shift()` call now passes `supplier_id=None, supplier_name=None`.

**Note:** The `_render_supplier_selector` function in this file (line 1600) is
unused dead code — left as-is, can be removed in a cleanup round.

### Patch E — `stock_pipeline.py` (📄 Stock POs subtab)

**New feature.** Already wired to the Stock tab radio (placeholder existed at
line 329). Replaced the minimal placeholder function with full implementation:

- Filter: search by PO no / supplier name; status (All / SENT / ACKNOWLEDGED /
  RECEIVED / CANCELLED); time window (30 days / 90 days / 1 year / All).
- Metrics row: PO count, Open/Awaiting count, Received count, Total Value.
- Per-PO expander: status badge, line items table (with order_no, patient_name,
  supplier ref per line), and action buttons.
- Actions on open POs:
  - 📲 **Resend WhatsApp** — pre-filled message with supplier number
  - 📧 **Resend Mail** — mailto link with subject + body
  - 📊 **Excel** — downloadable XLSX with all lines
  - ❌ **Cancel PO** — two-click confirm; flips PO + items to CANCELLED, resets
    linked PO_SENT order_lines back to PENDING so they can be re-ordered.

## Smoke test sequence

Run all 9 steps before declaring this patch good.

### Stock flow

1. Open **Production → 📦 Stock**.
2. Select 1-2 stock lines (Active Pipeline view).
3. In the bottom Replenishment Order panel: pick supplier, set qty, click
   **Send PO**. PO number toasted. Lines now show `🔁 Replenishment: PO_SENT`.
4. On each PO_SENT line, you should see an **amber banner** "⏳ Awaiting
   supplier confirmation" and a **pre-expanded** "📋 Save supplier reference"
   panel. Confirm visible without clicking anything.
5. Enter a supplier ref number in the text field. Click **Save Ref**.
6. Line should disappear from Active Pipeline (it's now ORDERED). Open
   **Production → 📥 Procurement Queue**. Line appears here.

### Supplier (RX) flow

7. Find an RX order with VENDOR or EXTERNAL_LAB route and R+L lens lines.
   Open **Production → 🏭 Supplier** (or 🧪 External Supplier).
8. The order card should show `ORDER_PLACED` for both R+L. Enter a supplier
   ref in the combined input. Click **Save Ref**.
9. Both R+L should now advance to `SUPPLIER_CONFIRMED`. Open **Procurement
   Queue** — both lines appear there together.

### Procurement → Receive

10. In Procurement Queue, tick the lines. Pick supplier, enter invoice/challan
    no, price. Click **Save Purchase**.
11. Lines disappear from Procurement Queue (PA now exists with price > 0).
12. Open **🏭 Supplier** tab — those RX lines now show `READY_FOR_BILLING`.

### Stock POs tab

13. Open **Production → 📦 Stock**. Switch to **📄 Stock POs** radio.
14. Most recent PO should appear at the top with status. Expand it.
15. Verify line items table shows order_no + patient_name per line.
16. For an open (SENT) PO: try Resend WhatsApp (opens wa.me link), Resend
    Mail (opens mailto), Excel download (XLSX file).
17. Cancel a test PO. Confirms with Yes/No. Linked PO_SENT order_lines should
    revert to PENDING.

### Backoffice (no supplier picker)

18. Open a backoffice assignment view. Try the "Shift route" UI on any line.
    Switch route to VENDOR or EXTERNAL_LAB. You should see a **caption**, NOT
    a supplier dropdown.
19. Apply shift. Line moves to the new route. Open Supplier/External Supplier
    tab — line appears there for the procurement team to pick supplier.

## Rollback

Drop the previous versions of the four files. No SQL, no schema, no migrations.

## What did NOT change

- `replenishment_panel.py` — untouched. The blank PO flow from Phase 3 is
  unaffected.
- `production_page.py` — untouched. Tab structure unchanged.
- `procurement_consolidation.py`, `purchase_register.py` — untouched.
- All punching, billing, dispatch, challan files — untouched.
- Database schema — no changes.
- Existing data — no migrations. Existing `NOT_READY` PAs were previously
  leaking; the new filter immediately excludes them from the queue (correct).
  No data fix needed.

## Known limitations

- Patch B's auto-expand applies to PO_SENT lines globally. If a PO_SENT line
  has many R+L+I split items, the expander stack gets long. Acceptable for
  now — pagination is a future enhancement.
- Stock PO cancel resets `replenishment_status` only on lines still at
  `PO_SENT` (or empty). Lines already at ORDERED stay ORDERED — cancel from
  the line itself if you need to fully roll back.
- Issue #2 (tab jumping) is only partially addressed. Save Ref in Stock and
  R+L Supplier preserve the panel correctly; other reruns across the
  production_page may still bounce back to Dashboard.
