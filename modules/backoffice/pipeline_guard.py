"""
pipeline_guard.py
═════════════════════════════════════════════════════════════════════════════
Smart pipeline governance — AI-driven permission engine for DV Optical.

PHILOSOPHY
──────────
Each order line lives at a specific DEPTH in the production pipeline.
The deeper it is, the more dangerous any edit becomes.  This module asks:

    "Given what has already happened to this line,
     is the requested action safe — and if not, exactly what must be
     undone first, and who is authorised to approve it?"

DEPTH LEVELS (lowest → highest risk)
──────────────────────────────────────
 0  PUNCHED        Order line exists, no job card yet → freely editable
 1  JOB_CREATED    Job card created, blank NOT yet picked → power edit OK with warning
 2  JOB_PRINTED    Printed but blank not picked → reset job (no inventory impact)
 3  BLANK_ALLOTTED Blank picked / inventory deducted → must cancel job card first
 4  IN_PRODUCTION  Past PRODUCTION_PICKED (surfacing started) → no power edit; form/base changes only
 5  COATING        Hardcoat / Colouring / ARC started → even form changes blocked
 6  PURCHASE_SENT  Supplier PO raised (EXTERNAL_LAB / VENDOR route) → everything locked
 7  PURCHASE_DONE  PO received / INSPECTION passed → immutable

BACKSTEP GOVERNANCE
────────────────────
Admin can move a job BACK to an earlier stage, but each backstep carries
cascade consequences that are computed and shown before confirming.
Example:  ARC_RECEIVED → PRODUCTION_PICKED  means the ARC batch must be
          voided, coating charges reversed, and blank qty restored.

For each backstep the module returns:
  • is_allowed       bool
  • requires_role    "admin" | "manager" | None
  • cascade_warnings list[str]   — things that WILL change automatically
  • manual_steps     list[str]   — things the operator MUST do manually
  • blocking_reasons list[str]   — why it cannot happen at all

ORDER EDIT LOCK
────────────────
Once an order reaches CONFIRMED (= saved in backoffice) the punching /
orders screen shows it read-only with a contextual message explaining
exactly what must be done first (cancel job card, cancel PO, etc.)
based on the actual pipeline depth of each line.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# STAGE CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Job master stages in sequence order (used for depth comparison)
JOB_STAGE_ORDER = [
    "JOB_CREATED",
    "JOB_PRINTED",
    "PRODUCTION_PICKED",
    "PRODUCTION_COMPLETED",
    "INSPECTION",
    "HARDCOAT_PICKED",
    "HARDCOAT_COMPLETED",
    "COLOURING_PICKED",
    "COLOURING_COMPLETED",
    "SENT_TO_ARC",
    "ARC_RECEIVED",
    "FINAL_QC",
    "READY_FOR_PACK",
    "FITTING_PENDING",
    "FITTING_SENT",
    "FITTING_RECEIVED",
    "FITTING_DONE",
    "DISPATCHED",
]

_STAGE_DEPTH = {s: i for i, s in enumerate(JOB_STAGE_ORDER)}

# Supplier PO stages that mean goods are in transit / received
PO_LOCKED_STATUSES = {"SENT", "ACKNOWLEDGED", "PARTIAL", "RECEIVED",
                       "INSPECTION", "COMPLETE", "READY_TO_BILL", "CLOSED"}

# Stages at which a BACKSTEP from admin is permissible at all
BACKSTEP_ALLOWED_FROM = {
    "JOB_PRINTED":          "JOB_CREATED",
    "PRODUCTION_PICKED":    "JOB_PRINTED",
    "PRODUCTION_COMPLETED": "PRODUCTION_PICKED",
    "INSPECTION":           "PRODUCTION_COMPLETED",
    "HARDCOAT_PICKED":      "INSPECTION",
    "HARDCOAT_COMPLETED":   "HARDCOAT_PICKED",
    "COLOURING_PICKED":     "INSPECTION",
    "COLOURING_COMPLETED":  "COLOURING_PICKED",
    "SENT_TO_ARC":          "INSPECTION",
    "ARC_RECEIVED":         "SENT_TO_ARC",      # allowed but with strong warnings
    "FINAL_QC":             "ARC_RECEIVED",
    "READY_FOR_PACK":       "FINAL_QC",
    "FITTING_PENDING":      "READY_FOR_PACK",
}

# These stages require ADMIN role for a backstep (not just manager)
BACKSTEP_REQUIRES_ADMIN = {
    "PRODUCTION_PICKED",   # blank already deducted from inventory
    "SENT_TO_ARC",
    "ARC_RECEIVED",
    "READY_FOR_PACK",
    "FINAL_QC",
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LineDepth:
    """Everything the guard knows about a single order line's pipeline state."""
    line_id:        str
    eye_side:       str          = ""
    product_name:   str          = ""

    # Job master
    job_stage:      Optional[str] = None   # current_stage from job_master
    blank_allotted: bool          = False  # blank_allocations row exists
    job_master_id:  Optional[str] = None

    # Supplier PO
    po_status:      Optional[str] = None   # status from supplier_orders
    po_id:          Optional[int] = None
    po_type:        Optional[str] = None   # VENDOR | EXTERNAL_LAB | REPLENISHMENT

    # Computed
    depth:          int = 0   # 0-7 per the PHILOSOPHY above


@dataclass
class EditPermission:
    """Result of asking 'can I edit field X on this order?'"""
    can_edit_power:         bool = True   # SPH / CYL / AXIS / ADD
    can_edit_product:       bool = True   # change product / SKU / lens params
    can_edit_qty:           bool = True   # quantity
    can_edit_price:         bool = True   # price / discount
    can_add_lines:          bool = True   # add new order lines
    can_remove_lines:       bool = True   # remove order lines

    blocking_message:       str  = ""     # shown prominently to user
    guidance:               str  = ""     # step-by-step what to do first
    unlock_steps:           List[str] = field(default_factory=list)
    required_role:          Optional[str] = None   # None = any user


@dataclass
class BackstepResult:
    """Result of asking 'can admin move job from stage A back to stage B?'"""
    is_allowed:       bool
    requires_role:    str            = "manager"
    cascade_warnings: List[str]      = field(default_factory=list)
    manual_steps:     List[str]      = field(default_factory=list)
    blocking_reasons: List[str]      = field(default_factory=list)
    auto_actions:     List[str]      = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS (all best-effort — guard never crashes the UI)
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql: str, params: dict):
    try:
        from modules.sql_adapter import run_query
        return run_query(sql, params) or []
    except Exception:
        return []


def _fetch_line_depth(line_id: str) -> LineDepth:
    """Query DB to determine the pipeline depth of a single order line."""
    ld = LineDepth(line_id=line_id)
    if not line_id:
        return ld

    # ── Job master ────────────────────────────────────────────────────────
    jm_rows = _q(
        "SELECT id, current_stage FROM job_master "
        "WHERE order_line_id = %(l)s::uuid AND is_closed = FALSE LIMIT 1",
        {"l": line_id}
    )
    if jm_rows:
        ld.job_master_id = str(jm_rows[0].get("id") or "")
        ld.job_stage     = jm_rows[0].get("current_stage")

    # ── Blank allocation ──────────────────────────────────────────────────
    ba_rows = _q(
        "SELECT id FROM blank_allocations WHERE order_line_id = %(l)s::uuid LIMIT 1",
        {"l": line_id}
    )
    ld.blank_allotted = bool(ba_rows)

    # ── Supplier PO ───────────────────────────────────────────────────────
    po_rows = _q(
        """SELECT so.id, so.status, so.po_type
           FROM supplier_order_items soi
           JOIN supplier_orders so ON so.id = soi.supplier_order_id
           WHERE soi.customer_line_id::text = %(l)s
             AND so.status NOT IN ('CANCELLED','DRAFT')
           ORDER BY so.created_at DESC LIMIT 1""",
        {"l": line_id}
    )
    if po_rows:
        ld.po_id     = po_rows[0].get("id")
        ld.po_status = po_rows[0].get("status")
        ld.po_type   = po_rows[0].get("po_type")

    # ── Compute depth ─────────────────────────────────────────────────────
    ld.depth = _compute_depth(ld)
    return ld


def _compute_depth(ld: LineDepth) -> int:
    """Map pipeline state → integer depth 0-7."""
    # PO fully locked
    if ld.po_status in ("RECEIVED", "INSPECTION", "COMPLETE",
                         "READY_TO_BILL", "CLOSED"):
        return 7
    # PO raised (in transit)
    if ld.po_status in ("SENT", "ACKNOWLEDGED", "PARTIAL"):
        return 6
    # Coating stages
    if ld.job_stage in ("HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
                         "COLOURING_PICKED", "COLOURING_COMPLETED",
                         "SENT_TO_ARC", "ARC_RECEIVED",
                         "FINAL_QC", "READY_FOR_PACK",
                         "FITTING_PENDING", "FITTING_SENT",
                         "FITTING_RECEIVED", "FITTING_DONE"):
        return 5
    # Surfacing in progress
    if ld.job_stage in ("PRODUCTION_PICKED", "PRODUCTION_COMPLETED",
                         "INSPECTION"):
        return 4
    # Blank allotted
    if ld.blank_allotted or ld.job_stage == "JOB_PRINTED":
        return 3
    # Job card created only
    if ld.job_stage == "JOB_CREATED":
        return 1
    # Nothing done
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GUARD — ORDER EDIT PERMISSIONS
# ─────────────────────────────────────────────────────────────────────────────

def get_order_edit_permission(order: dict) -> EditPermission:
    """
    Given a loaded order dict (with lines), compute the tightest
    EditPermission across ALL its lines.

    The tightest (most restrictive) line wins — if even one line
    has a blank allotted, the whole order shows the blank-cancel guidance.
    """
    perm = EditPermission()   # start fully open

    lines = order.get("lines") or []
    if not lines:
        return perm

    max_depth = 0
    critical_line: Optional[LineDepth] = None

    for line in lines:
        lid = str(line.get("line_id") or line.get("id") or "")
        if not lid:
            continue
        ld = _fetch_line_depth(lid)
        ld.product_name = line.get("product_name", "")
        ld.eye_side     = str(line.get("eye_side") or "").upper()
        if ld.depth > max_depth:
            max_depth     = ld.depth
            critical_line = ld

    if max_depth == 0:
        return perm   # fully open

    # ── Build permission based on max depth ───────────────────────────────
    eye = critical_line.eye_side if critical_line else "?"
    prod = critical_line.product_name if critical_line else ""

    if max_depth >= 7:
        # Purchase received / complete — NOTHING editable
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_edit_qty     = False
        perm.can_edit_price   = False
        perm.can_add_lines    = False
        perm.can_remove_lines = False
        perm.required_role    = "admin"
        perm.blocking_message = (
            f"🔒 Order locked — goods received from supplier "
            f"(PO status: {critical_line.po_status})."
        )
        perm.guidance = (
            "All lines are locked because a purchase order has been received. "
            "No changes are possible at this stage. "
            "If there is an error, raise a supplier return or create a new order."
        )
        perm.unlock_steps = [
            "Contact supplier to raise a return/replacement PO",
            "Admin can cancel the received PO in Supplier Panel only if goods not yet billed",
            "Create a new replacement order if prescription changed",
        ]

    elif max_depth == 6:
        # PO sent/in-transit — power locked, price/qty restricted
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_edit_qty     = False
        perm.required_role    = "manager"
        perm.blocking_message = (
            f"⚠️ Supplier PO is in transit (status: {critical_line.po_status}). "
            f"Power and product cannot be changed."
        )
        perm.guidance = (
            "A purchase order has been sent to the supplier. "
            "Power, product and quantity are locked. "
            "Price/discount edits are still allowed for billing corrections."
        )
        perm.unlock_steps = [
            "Cancel the supplier PO in Supplier Panel (if supplier not yet dispatched)",
            "Then power/product changes will unlock automatically",
        ]

    elif max_depth >= 5:
        # Coating in progress
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_edit_qty     = False
        perm.can_remove_lines = False
        perm.required_role    = "manager"
        stage_label = critical_line.job_stage or "coating"
        perm.blocking_message = (
            f"🔒 {eye} eye is in coating stage ({stage_label}). "
            f"Power and product are locked."
        )
        perm.guidance = (
            "Lens is currently in the coating/ARC process. "
            "Power and product cannot be changed. "
            "If there is an error, the job must be rejected and restarted from blank selection."
        )
        perm.unlock_steps = [
            f"Go to Production → {eye} Eye → use '↩️ Reject & Return Blank'",
            "Select rejection reason → job resets to JOB_CREATED",
            "Then power can be changed and a new blank selected",
        ]

    elif max_depth == 4:
        # Surfacing in progress
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_remove_lines = False
        perm.required_role    = "manager"
        perm.blocking_message = (
            f"🔒 {eye} eye is in surfacing ({critical_line.job_stage}). "
            f"Power is locked — lens is being ground."
        )
        perm.guidance = (
            "Surfacing has started. Changing prescription now would result in wrong lens. "
            "To change power, the blank must be rejected and a new blank selected."
        )
        perm.unlock_steps = [
            f"Go to Production → {eye} Eye → '↩️ Reject & Return Blank to Stock'",
            "This resets the job to JOB_CREATED and restores inventory",
            "Then change power and re-enter the job card",
        ]

    elif max_depth == 3:
        # Blank allotted or job printed — most common case
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.required_role    = None   # any user can see; but save is blocked
        perm.blocking_message = (
            f"⚠️ {eye} eye has a blank allotted "
            f"({'job card printed' if critical_line.job_stage == 'JOB_PRINTED' else 'blank picked from inventory'})."
        )
        perm.guidance = (
            "A blank has been allocated for this lens. "
            "To change power or product, the job card must be cancelled first "
            "so the blank is returned to inventory."
        )
        perm.unlock_steps = [
            f"Go to Backoffice → Documents tab → {eye} Eye Job Card",
            "Click '↩️ Reject & Return Blank to Stock' and select reason",
            "Job resets to JOB_CREATED → blank returns to stock",
            "Then change power here and enter new job card",
        ]

    elif max_depth == 1:
        # Job created but no blank yet — safe to edit with warning
        perm.can_edit_power   = True
        perm.can_edit_product = True
        perm.blocking_message = ""
        perm.guidance = (
            "⚠️ A job card exists but no blank has been picked yet. "
            "Changing power will update the job card — please re-verify surfacing calculations after saving."
        )

    return perm


# ─────────────────────────────────────────────────────────────────────────────
# FIELD-LEVEL GUARD — for granular per-field lock in the edit panel
# ─────────────────────────────────────────────────────────────────────────────

def field_is_editable(field_name: str, perm: EditPermission) -> bool:
    """True if field_name is editable given the EditPermission."""
    power_fields   = {"sph", "cyl", "axis", "add_power", "sphere", "cylinder"}
    product_fields = {"product_id", "product_name", "sku", "lens_params",
                      "frame_group", "colour_mix", "batch_no", "category"}
    qty_fields     = {"quantity", "billing_qty"}
    price_fields   = {"unit_price", "discount_percent", "discount_amount",
                      "gst_percent", "total_price"}

    fn = field_name.lower()
    if fn in power_fields:   return perm.can_edit_power
    if fn in product_fields: return perm.can_edit_product
    if fn in qty_fields:     return perm.can_edit_qty
    if fn in price_fields:   return perm.can_edit_price
    return True   # unknown fields default open


# ─────────────────────────────────────────────────────────────────────────────
# BACKSTEP GOVERNANCE
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_backstep(job_id: str, from_stage: str, to_stage: str,
                      eye_side: str = "") -> BackstepResult:
    """
    Evaluate whether admin can move a job_master record back from
    from_stage → to_stage.

    Returns BackstepResult with full consequence analysis.
    """
    result = BackstepResult(is_allowed=False)

    from_depth = _STAGE_DEPTH.get(from_stage, 99)
    to_depth   = _STAGE_DEPTH.get(to_stage, 99)

    # Must be a genuine backstep
    if to_depth >= from_depth:
        result.blocking_reasons.append(
            f"{to_stage} is not earlier than {from_stage} — this is not a backstep."
        )
        return result

    # Only adjacent-stage backstep allowed (no skipping multiple stages)
    allowed_target = BACKSTEP_ALLOWED_FROM.get(from_stage)
    if allowed_target != to_stage:
        result.blocking_reasons.append(
            f"Can only move back to {allowed_target or '(none)'} from {from_stage}. "
            f"Cannot skip directly to {to_stage}. "
            f"Use consecutive backsteps if needed."
        )
        return result

    # Role requirement
    result.requires_role = (
        "admin" if from_stage in BACKSTEP_REQUIRES_ADMIN else "manager"
    )
    result.is_allowed = True

    # ── Build cascade analysis per from_stage ────────────────────────────
    eye = f" ({eye_side} eye)" if eye_side else ""

    if from_stage == "JOB_PRINTED":
        result.cascade_warnings = [
            f"Job card status{eye} will be reset from JOB_PRINTED → JOB_CREATED",
        ]
        result.auto_actions = [
            "job_master.current_stage reset to JOB_CREATED",
            "job_stage_events record added (BACKSTEP by admin)",
        ]

    elif from_stage == "PRODUCTION_PICKED":
        result.cascade_warnings = [
            f"⚠️ Blank inventory will be RESTORED (+1){eye} — blank returns to stock",
            "Job card reset to JOB_CREATED — blank must be re-selected",
            "blank_allocations row will be deleted",
        ]
        result.auto_actions = [
            "blank_inventory qty_right / qty_left incremented by 1",
            "blank_allocations row deleted for this line",
            "job_master reset to JOB_PRINTED",
            "reprocess_count incremented",
        ]
        result.manual_steps = [
            "Verify blank is physically returned to storage location",
            "Re-enter job card and select correct blank",
        ]

    elif from_stage == "PRODUCTION_COMPLETED":
        result.cascade_warnings = [
            f"Job{eye} reverts to PRODUCTION_PICKED — surfacing output voided",
        ]
        result.auto_actions = [
            "job_master reset to PRODUCTION_PICKED",
            "No inventory change — blank not yet returned",
        ]
        result.manual_steps = [
            "Physically return the half-processed lens to the surfacing department",
            "Re-do surfacing and advance stage again when complete",
        ]

    elif from_stage == "INSPECTION":
        result.cascade_warnings = [
            f"Job{eye} reverts to PRODUCTION_COMPLETED — inspection undone",
        ]
        result.auto_actions = ["job_master reset to PRODUCTION_COMPLETED"]
        result.manual_steps = ["Re-inspect lens and advance stage again"]

    elif from_stage in ("HARDCOAT_PICKED", "HARDCOAT_COMPLETED"):
        result.cascade_warnings = [
            f"⚠️ Hardcoat stage reversed{eye} — coating department must be notified",
        ]
        result.auto_actions = [f"job_master reset to INSPECTION"]
        result.manual_steps = [
            "Inform hardcoat department to stop processing",
            "If hardcoat already applied, a new blank may be required",
        ]

    elif from_stage in ("COLOURING_PICKED", "COLOURING_COMPLETED"):
        result.cascade_warnings = [
            f"⚠️ Colouring reversed{eye} — colour department must be notified",
            "Any uploaded colour photo will be removed",
        ]
        result.auto_actions = [
            "job_master reset to INSPECTION",
            "colour_final_photo cleared from lens_params",
        ]
        result.manual_steps = [
            "Notify colour department to hold/return lens",
            "If colour already applied, blank must be rejected and restarted",
        ]

    elif from_stage == "SENT_TO_ARC":
        result.cascade_warnings = [
            f"⚠️ ARC reversal{eye} — lens is physically at ARC vendor",
            "This backstep only changes the system record; lens may still be with vendor",
        ]
        result.requires_role = "admin"
        result.auto_actions = ["job_master reset to INSPECTION"]
        result.manual_steps = [
            "Contact ARC vendor to confirm lens has NOT been processed",
            "Arrange physical return of lens from ARC vendor",
            "If ARC already applied, a new blank + full re-work is required",
        ]

    elif from_stage == "ARC_RECEIVED":
        result.cascade_warnings = [
            f"⚠️ ARC_RECEIVED reversed{eye}",
            "System will show lens as SENT_TO_ARC again — ARC may already be applied",
            "This is a RECORD CORRECTION only — does NOT undo the physical ARC",
        ]
        result.requires_role = "admin"
        result.auto_actions = ["job_master reset to SENT_TO_ARC"]
        result.manual_steps = [
            "This should only be used to correct a scan error",
            "If ARC needs to be re-done, use 'Reject & Return Blank' instead",
        ]

    elif from_stage in ("FINAL_QC", "READY_FOR_PACK"):
        result.cascade_warnings = [
            f"⚠️ Rolling back from {from_stage}{eye} — order may already be in dispatch queue",
            "Billing status may need to be checked and reversed",
        ]
        result.requires_role = "admin"
        result.auto_actions = [f"job_master reset to {BACKSTEP_ALLOWED_FROM[from_stage]}"]
        result.manual_steps = [
            "Check if billing invoice has been raised — reverse if needed",
            "Remove lens from dispatch tray",
            "Re-QC the lens and advance stage again",
        ]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# UI HELPERS — render guards directly in Streamlit
# ─────────────────────────────────────────────────────────────────────────────

def render_edit_lock_banner(perm: EditPermission, context: str = "order"):
    """Render the lock banner + unlock guidance in Streamlit."""
    import streamlit as st

    if not perm.blocking_message and not perm.guidance:
        return

    # Depth-colour coding
    if not perm.can_edit_power and not perm.can_edit_product:
        bg, border, icon = "#1a0a0a", "#ef4444", "🔒"
    elif not perm.can_edit_power:
        bg, border, icon = "#1c1107", "#f59e0b", "⚠️"
    else:
        bg, border, icon = "#0a1628", "#3b82f6", "ℹ️"

    st.markdown(
        f"<div style='background:{bg};border:1px solid {border};border-radius:8px;"
        f"padding:12px 16px;margin-bottom:10px'>"
        f"<div style='color:{border};font-weight:700;margin-bottom:4px'>"
        f"{icon} {perm.blocking_message}</div>"
        + (f"<div style='color:#94a3b8;font-size:0.8rem'>{perm.guidance}</div>"
           if perm.guidance else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    if perm.unlock_steps:
        with st.expander("🔓 How to unlock for editing", expanded=False):
            for i, step in enumerate(perm.unlock_steps, 1):
                st.markdown(
                    f"<div style='padding:3px 0;color:#e2e8f0;font-size:0.82rem'>"
                    f"<span style='color:#60a5fa;font-weight:700'>{i}.</span> {step}</div>",
                    unsafe_allow_html=True,
                )


def render_backstep_ui(job_id: str, current_stage: str, eye_side: str,
                       order_id: str):
    """
    Admin-only backstep panel rendered inside production_panel expander.
    Shows allowed backstep target, full consequence analysis, and confirm button.
    """
    import streamlit as st
    try:
        from modules.security.roles import has_role
    except Exception:
        def has_role(*args): return True   # fallback if roles module absent

    # Only show for admin / manager
    if not has_role("admin", "manager"):
        return

    target = BACKSTEP_ALLOWED_FROM.get(current_stage)
    if not target:
        return   # no backstep defined for this stage

    result = evaluate_backstep(job_id, current_stage, target, eye_side)

    with st.expander(f"🔙 Admin Backstep: {current_stage} → {target}", expanded=False):
        if not result.is_allowed:
            for r in result.blocking_reasons:
                st.error(r)
            return

        role_label = "🔑 Admin only" if result.requires_role == "admin" else "👔 Manager+"
        st.markdown(
            f"<div style='background:#1c1107;border:1px solid #f59e0b;border-radius:6px;"
            f"padding:10px 14px;margin-bottom:8px'>"
            f"<span style='color:#fbbf24;font-weight:700'>⚠️ Backstep Analysis</span>"
            f"<span style='color:#94a3b8;font-size:0.75rem;margin-left:8px'>{role_label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        if result.cascade_warnings:
            st.markdown("**Will happen automatically:**")
            for w in result.cascade_warnings:
                color = "#ef4444" if "⚠️" in w else "#94a3b8"
                st.markdown(
                    f"<div style='color:{color};font-size:0.8rem;padding:2px 0'>• {w}</div>",
                    unsafe_allow_html=True,
                )

        if result.manual_steps:
            st.markdown("**You must do manually:**")
            for s in result.manual_steps:
                st.markdown(
                    f"<div style='color:#fbbf24;font-size:0.8rem;padding:2px 0'>📋 {s}</div>",
                    unsafe_allow_html=True,
                )

        # Role gate
        if result.requires_role == "admin" and not has_role("admin"):
            st.error("🔑 Admin role required to execute this backstep.")
            return

        _confirm_key = f"backstep_confirm_{job_id}"
        _reason_key  = f"backstep_reason_{job_id}"

        reason = st.text_input(
            "Reason for backstep (required)",
            key=_reason_key,
            placeholder="e.g. Wrong base curve selected, customer changed prescription",
        )

        if not st.session_state.get(_confirm_key):
            if st.button(
                f"↩️ Move back to {target}",
                key=f"backstep_btn_{job_id}",
                disabled=not reason.strip(),
                use_container_width=True,
            ):
                st.session_state[_confirm_key] = True
                st.rerun()
        else:
            st.warning(
                f"Confirm: move **{current_stage}** → **{target}** for {eye_side} eye? "
                f"This cannot be undone automatically."
            )
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button("✅ Confirm Backstep", key=f"backstep_exec_{job_id}",
                             type="primary", use_container_width=True):
                    _execute_backstep(job_id, current_stage, target, reason, order_id)
                    st.session_state.pop(_confirm_key, None)
            with _c2:
                if st.button("✕ Cancel", key=f"backstep_cancel_{job_id}",
                             use_container_width=True):
                    st.session_state.pop(_confirm_key, None)
                    st.rerun()


def _execute_backstep(job_id: str, from_stage: str, to_stage: str,
                      reason: str, order_id: str):
    """Execute the DB side of a backstep + cascade actions."""
    import streamlit as st
    try:
        from modules.sql_adapter import run_write, run_scalar, run_query
    except Exception as e:
        st.error(f"DB unavailable: {e}")
        return

    try:
        # 1. Reset job_master stage
        run_write(
            "UPDATE job_master SET current_stage = %(ts)s, updated_at = NOW() "
            "WHERE id = %(j)s::uuid",
            {"ts": to_stage, "j": job_id}
        )

        # 2. Log backstep event
        run_write(
            """INSERT INTO job_stage_events
               (id, job_id, stage_code, department, remarks, created_at)
               VALUES (gen_random_uuid(), %(j)s::uuid, %(ts)s,
                       'ADMIN_BACKSTEP', %(r)s, NOW())""",
            {"j": job_id, "ts": to_stage, "r": f"[BACKSTEP from {from_stage}] {reason}"}
        )

        # 3. Cascade: restore blank inventory if moving back from PRODUCTION_PICKED
        if from_stage == "PRODUCTION_PICKED":
            _rows = run_query(
                "SELECT ba.blank_id, ba.eye_side FROM blank_allocations ba "
                "JOIN job_master jm ON jm.order_line_id = ba.order_line_id "
                "WHERE jm.id = %(j)s::uuid LIMIT 1",
                {"j": job_id}
            )
            if _rows:
                _bid = str(_rows[0].get("blank_id") or "")
                _eye = str(_rows[0].get("eye_side") or "")
                if _bid:
                    try:
                        from modules.sql_adapter import update_blank_quantity
                        update_blank_quantity(_bid, qty_change=+1,
                                              eye_side=_eye if _eye in ("R","L") else None)
                    except Exception:
                        pass
            # Delete blank_allocations row
            run_write(
                "DELETE FROM blank_allocations ba "
                "USING job_master jm "
                "WHERE jm.order_line_id = ba.order_line_id AND jm.id = %(j)s::uuid",
                {"j": job_id}
            )

        # 4. Cascade: clear colour photo if reversing past COLOURING
        if from_stage in ("COLOURING_COMPLETED", "COLOURING_PICKED"):
            try:
                run_write(
                    """UPDATE order_lines SET
                       lens_params = (COALESCE(lens_params, '{}')::jsonb
                                      - 'colour_final_photo')
                       FROM job_master jm
                       WHERE order_lines.id = jm.order_line_id
                         AND jm.id = %(j)s::uuid""",
                    {"j": job_id}
                )
            except Exception:
                pass

        # 5. Log to event logger
        try:
            from modules.backoffice.event_logger import log_event, EventType
            log_event(EventType.STAGE_ADVANCED, order_id=order_id,
                      details={"job_id": job_id, "backstep": True,
                               "from": from_stage, "to": to_stage,
                               "reason": reason}, source="admin")
        except Exception:
            pass

        st.success(
            f"✅ Backstep complete: {from_stage} → {to_stage}. "
            f"Reason logged: '{reason}'"
        )
        st.rerun()

    except Exception as e:
        st.error(f"❌ Backstep failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE — quick depth label for UI badges
# ─────────────────────────────────────────────────────────────────────────────

DEPTH_LABELS = {
    0: ("🟢 No job card", "#10b981"),
    1: ("🟡 Job card only", "#f59e0b"),
    3: ("🟠 Blank allotted", "#f97316"),
    4: ("🔴 In surfacing", "#ef4444"),
    5: ("🔴 In coating/ARC", "#dc2626"),
    6: ("🔒 PO in transit", "#7c3aed"),
    7: ("🔒 Purchase received", "#6b21a8"),
}

def depth_badge(depth: int) -> tuple[str, str]:
    """Return (label, color) for a depth value."""
    for d in sorted(DEPTH_LABELS.keys(), reverse=True):
        if depth >= d:
            return DEPTH_LABELS[d]
    return ("🟢 Open", "#10b981")


__all__ = [
    "get_order_edit_permission",
    "field_is_editable",
    "evaluate_backstep",
    "render_edit_lock_banner",
    "render_backstep_ui",
    "EditPermission",
    "BackstepResult",
    "LineDepth",
    "depth_badge",
]
