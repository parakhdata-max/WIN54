# INTEGRATION GUIDE — audit_fixes.py
# Where to wire each fix into existing files
# =====================================================================

# ─────────────────────────────────────────────────────────────────────
# FIX 1 — RETAIL EDIT LOCK (timing gap)
# File: modules/backoffice/order_edit_view.py
# Function: _render_order_edit_panel()
# Replace the existing status-only check with:
# ─────────────────────────────────────────────────────────────────────

# BEFORE (line ~1336):
# elif _cur_st in RETAIL_EDIT_LOCKED_AFTER:
#     ... generic banner ...
#     return

# AFTER — add at TOP of _render_order_edit_panel(), before anything else:
from modules.security.audit_fixes import order_backoffice_touched, render_backoffice_lock_banner

def _render_order_edit_panel(order, editable):
    # NEW: DB signal check — catches timing gap (PENDING but backoffice has touched it)
    touched, signal = order_backoffice_touched(order)
    if touched:
        render_backoffice_lock_banner(signal, order)
        return   # stops ALL edit UI from rendering

    # ... rest of existing function unchanged ...


# ─────────────────────────────────────────────────────────────────────
# FIX 2 — PRICE OVERRIDE ROLE ENFORCEMENT
# File: modules/backoffice/backoffice_ui.py
# Location: wherever unit_price is written to line dict before save
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import enforce_price_override_role, render_price_block_banner

# Before any line["unit_price"] = new_value write:
allowed, block_reason = enforce_price_override_role(
    new_price      = new_unit_price,
    original_price = float(line.get("unit_price") or 0),
    order_no       = order.get("order_no", ""),
    line_name      = line.get("product_name", ""),
)
if not allowed:
    render_price_block_banner(block_reason)
    return  # or st.stop()
# ... proceed with save


# ─────────────────────────────────────────────────────────────────────
# FIX 3 — CUMULATIVE DISCOUNT CHECK
# File: modules/backoffice/backoffice_ui.py
# Location: just before the final SAVE button is processed
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import (
    calculate_effective_discount, render_discount_approval_gate
)

# Inside the save flow (before persist_order is called):
disc_info = calculate_effective_discount(order.get("lines", []))
if disc_info["over_threshold"]:
    approved, meta = render_discount_approval_gate(
        disc_info, order.get("order_no",""), f"save_{order.get('order_no','')}"
    )
    if not approved:
        st.stop()   # halt save — dialog stays open waiting for manager
# ... proceed with save


# ─────────────────────────────────────────────────────────────────────
# FIX 4 — FINANCIAL RECORD CHECK BEFORE DELETE
# File: modules/backoffice/backoffice.py  (render_cancel_order_panel)
# Location: inside the cancel expander, before the confirm button
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import (
    check_order_has_financial_records, render_financial_delete_block
)

# Inside render_cancel_order_panel(), for HARD DELETE (admin only):
# (For soft cancel/CANCELLED status, financial records are allowed — this
#  only applies when status would be truly deleted from DB)
if is_hard_delete:   # wherever hard delete is triggered
    fin_blocked, fin_reasons = check_order_has_financial_records(order)
    if fin_blocked:
        render_financial_delete_block(fin_reasons)
        return


# ─────────────────────────────────────────────────────────────────────
# FIX 5 — FORMAL ARC BACKSTEP EVENT
# File: modules/core/pipeline_guard.py  (execute_backstep_db)
# Location: after the DB stage reset succeeds, when from_stage has ARC
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import log_arc_backstep_event

# Inside execute_backstep_db(), after the UPDATE job_master succeeds:
if from_stage in ("SENT_TO_ARC", "ARC_RECEIVED"):
    # Fetch vendor info
    arc_rows = _q("""
        SELECT so.vendor_name, so.vendor_contact, so.po_ref
        FROM supplier_orders so
        JOIN supplier_order_items soi ON soi.supplier_order_id = so.id
        JOIN job_master jm ON jm.order_line_id = soi.customer_line_id
        WHERE jm.id = %(j)s::uuid
        ORDER BY so.created_at DESC LIMIT 1
    """, {"j": job_id})
    vendor_name    = arc_rows[0].get("vendor_name","") if arc_rows else ""
    vendor_contact = arc_rows[0].get("vendor_contact","") if arc_rows else ""
    po_ref         = arc_rows[0].get("po_ref","") if arc_rows else ""

    log_arc_backstep_event(
        job_id         = job_id,
        from_stage     = from_stage,
        order_id       = order_id,
        eye_side       = eye_side,
        reason         = reason,
        vendor_name    = vendor_name,
        vendor_contact = vendor_contact,
        po_ref         = po_ref,
    )


# ─────────────────────────────────────────────────────────────────────
# FIX 6 — PO CANCEL STOCK CHECK
# File: modules/backoffice/supplier_panel.py  (_render_po_list)
# Location: inside the cancel PO button section, before allowing cancel
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import render_po_cancel_stock_check
from modules.security.business_guards import render_po_cancel_guard

# Replace the existing simple cancel buttons with:
# For SENT (and ACKNOWLEDGED / PARTIAL) status:
if status in ("DRAFT", "SENT", "ACKNOWLEDGED", "PARTIAL"):
    if allow_cancel:
        # Step 1: stock check
        stock_safe = render_po_cancel_stock_check(po_id_int)
        if stock_safe:
            # Step 2: role + reason + confirm (from business_guards)
            confirmed = render_po_cancel_guard(
                po_id_int, po_ref, status, vendor_name, order_id
            )
            if confirmed:
                run_write("UPDATE supplier_orders SET status='CANCELLED' WHERE id=%(id)s",
                          {"id": po_id_int})
                st.success("✅ PO cancelled")
                st.rerun()


# ─────────────────────────────────────────────────────────────────────
# FIX 7 — PREVIEW AS ROLE
# File: modules/security/permission_designer_ui.py  (_render_settings_tab)
# Location: add as last section in Settings tab
# ─────────────────────────────────────────────────────────────────────

from modules.security.audit_fixes import render_preview_as_role

# In _render_settings_tab(), after existing settings sections:
st.markdown("---")
render_preview_as_role()
