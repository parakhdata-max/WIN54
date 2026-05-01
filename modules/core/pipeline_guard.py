"""
modules/core/pipeline_guard.py
═══════════════════════════════════════════════════════════════════════════════
Smart pipeline governance — PURE LOGIC layer.
No Streamlit imports. Usable from retail, backoffice, API, documents.

Place at:  modules/core/pipeline_guard.py

The UI render functions (render_edit_lock_banner, render_backstep_ui)
live in modules/backoffice/pipeline_guard_ui.py and import from here.

DEPTH LEVELS
─────────────
 0  PUNCHED        Order line exists, no job card            → freely editable
 1  JOB_CREATED    Job card created, blank NOT yet picked    → power edit OK with warning
 3  BLANK_ALLOTTED Blank picked / inventory deducted         → must cancel job card first
 4  IN_PRODUCTION  Surfacing started (PRODUCTION_PICKED+)    → no power edit
 5  COATING        Hardcoat / Colouring / ARC started        → form changes also blocked
 6  PURCHASE_SENT  Supplier PO raised and in transit         → power+product+qty locked
 7  PURCHASE_DONE  PO received / inspection passed           → fully immutable
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# STAGE CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

JOB_STAGE_ORDER = [
    "JOB_CREATED", "JOB_PRINTED",
    "PRODUCTION_PICKED", "PRODUCTION_COMPLETED", "INSPECTION",
    "HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
    "COLOURING_PICKED", "COLOURING_COMPLETED",
    "SENT_TO_ARC", "ARC_RECEIVED",
    "FINAL_QC", "READY_FOR_PACK",
    "FITTING_PENDING", "FITTING_SENT", "FITTING_RECEIVED", "FITTING_DONE",
    "DISPATCHED",
]
_STAGE_DEPTH = {s: i for i, s in enumerate(JOB_STAGE_ORDER)}

PO_LOCKED_STATUSES = {
    "SENT", "ACKNOWLEDGED", "PARTIAL", "RECEIVED",
    "INSPECTION", "COMPLETE", "READY_TO_BILL", "CLOSED"
}

# Valid backstep targets — only adjacent stage, no skipping
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
    "ARC_RECEIVED":         "SENT_TO_ARC",
    "FINAL_QC":             "ARC_RECEIVED",
    "READY_FOR_PACK":       "FINAL_QC",
    "FITTING_PENDING":      "READY_FOR_PACK",
}

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
    """Pipeline state of a single order line, as read from DB."""
    line_id:        str
    eye_side:       str           = ""
    product_name:   str           = ""
    job_stage:      Optional[str] = None
    blank_allotted: bool          = False
    job_master_id:  Optional[str] = None
    po_status:      Optional[str] = None
    po_id:          Optional[int] = None
    po_type:        Optional[str] = None
    depth:          int           = 0


@dataclass
class EditPermission:
    """What is editable on an order, given its deepest pipeline state."""
    can_edit_power:     bool = True
    can_edit_product:   bool = True
    can_edit_qty:       bool = True
    can_edit_price:     bool = True
    can_add_lines:      bool = True
    can_remove_lines:   bool = True
    blocking_message:   str  = ""
    guidance:           str  = ""
    unlock_steps:       List[str] = field(default_factory=list)
    required_role:      Optional[str] = None
    # Raw depth for UI colour-coding
    max_depth:          int = 0
    critical_line:      Optional[LineDepth] = None


@dataclass
class BackstepResult:
    """Full consequence analysis for an admin backstep request."""
    is_allowed:       bool
    requires_role:    str       = "manager"
    cascade_warnings: List[str] = field(default_factory=list)
    manual_steps:     List[str] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    auto_actions:     List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _q(sql: str, params: dict) -> list:
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

    # Job master
    jm = _q(
        "SELECT id, current_stage FROM job_master "
        "WHERE order_line_id = %(l)s::uuid AND is_closed = FALSE LIMIT 1",
        {"l": line_id}
    )
    if jm:
        ld.job_master_id = str(jm[0].get("id") or "")
        ld.job_stage     = jm[0].get("current_stage")

    # Blank allocation
    ba = _q(
        "SELECT id FROM blank_allocations WHERE order_line_id = %(l)s::uuid LIMIT 1",
        {"l": line_id}
    )
    ld.blank_allotted = bool(ba)

    # Supplier PO
    po = _q(
        """SELECT so.id, so.status, so.po_type
           FROM supplier_order_items soi
           JOIN supplier_orders so ON so.id = soi.supplier_order_id
           WHERE soi.customer_line_id = %(l)s::uuid
             AND so.status NOT IN ('CANCELLED','DRAFT')
           ORDER BY so.created_at DESC LIMIT 1""",
        {"l": line_id}
    )
    if po:
        ld.po_id     = po[0].get("id")
        ld.po_status = po[0].get("status")
        ld.po_type   = po[0].get("po_type")

    ld.depth = _compute_depth(ld)
    return ld


def _compute_depth(ld: LineDepth) -> int:
    if ld.po_status in ("RECEIVED", "INSPECTION", "COMPLETE",
                         "READY_TO_BILL", "CLOSED"):
        return 7
    if ld.po_status in ("SENT", "ACKNOWLEDGED", "PARTIAL"):
        return 6
    if ld.job_stage in ("HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
                         "COLOURING_PICKED", "COLOURING_COMPLETED",
                         "SENT_TO_ARC", "ARC_RECEIVED",
                         "FINAL_QC", "READY_FOR_PACK",
                         "FITTING_PENDING", "FITTING_SENT",
                         "FITTING_RECEIVED", "FITTING_DONE"):
        return 5
    if ld.job_stage in ("PRODUCTION_PICKED", "PRODUCTION_COMPLETED", "INSPECTION"):
        return 4
    if ld.blank_allotted or ld.job_stage == "JOB_PRINTED":
        return 3
    if ld.job_stage == "JOB_CREATED":
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GUARD — ORDER EDIT PERMISSIONS
# ─────────────────────────────────────────────────────────────────────────────

def get_order_edit_permission(order: dict) -> EditPermission:
    """
    Compute the tightest EditPermission across ALL lines of an order.
    The deepest line wins — one blank-allotted line locks the whole order.
    """
    perm = EditPermission()
    lines = order.get("lines") or []
    if not lines:
        return perm

    max_depth = 0
    critical_line: Optional[LineDepth] = None

    for line in lines:
        lid = str(line.get("line_id") or line.get("id") or "")
        if not lid:
            continue
        ld              = _fetch_line_depth(lid)
        ld.product_name = line.get("product_name", "")
        ld.eye_side     = str(line.get("eye_side") or "").upper()
        if ld.depth > max_depth:
            max_depth     = ld.depth
            critical_line = ld

    perm.max_depth     = max_depth
    perm.critical_line = critical_line

    if max_depth == 0:
        return perm

    eye  = critical_line.eye_side     if critical_line else "?"
    prod = critical_line.product_name if critical_line else ""  # noqa: F841

    if max_depth >= 7:
        perm.can_edit_power = perm.can_edit_product = False
        perm.can_edit_qty   = perm.can_edit_price   = False
        perm.can_add_lines  = perm.can_remove_lines = False
        perm.required_role  = "admin"
        perm.blocking_message = (
            f"🔒 Order locked — goods received from supplier "
            f"(PO status: {critical_line.po_status})."
        )
        perm.guidance = (
            "All lines are locked because a purchase order has been received. "
            "No changes are possible. "
            "Raise a supplier return or create a new order for any correction."
        )
        perm.unlock_steps = [
            "Contact supplier to raise a return / replacement PO",
            "Admin can cancel the PO in Supplier Panel only if goods not yet billed",
            "Create a new replacement order if prescription changed",
        ]

    elif max_depth == 6:
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_edit_qty     = False
        perm.required_role    = "manager"
        perm.blocking_message = (
            f"⚠️ Supplier PO is in transit (status: {critical_line.po_status}). "
            "Power and product cannot be changed."
        )
        perm.guidance = (
            "A purchase order has been sent to the supplier. "
            "Power, product and quantity are locked. "
            "Price / discount edits are still allowed for billing corrections."
        )
        perm.unlock_steps = [
            "Cancel the supplier PO in Supplier Panel (only if supplier has not dispatched)",
            "Then power / product changes will unlock automatically",
        ]

    elif max_depth >= 5:
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_edit_qty     = False
        perm.can_remove_lines = False
        perm.required_role    = "manager"
        stage_label = critical_line.job_stage or "coating"
        perm.blocking_message = (
            f"🔒 {eye} eye is in coating / ARC stage ({stage_label}). "
            "Power and product are locked."
        )
        perm.guidance = (
            "Lens is in the coating / ARC process. "
            "To change power, the job must be rejected and restarted from blank selection."
        )
        perm.unlock_steps = [
            f"Go to Production → {eye} Eye → '↩️ Reject & Return Blank to Stock'",
            "Select rejection reason → job resets to JOB_CREATED, blank restored to inventory",
            "Then change power and enter a new job card",
        ]

    elif max_depth == 4:
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.can_remove_lines = False
        perm.required_role    = "manager"
        perm.blocking_message = (
            f"🔒 {eye} eye is in surfacing ({critical_line.job_stage}). "
            "Power is locked — lens is being ground."
        )
        perm.guidance = (
            "Surfacing has started. Changing prescription now would produce a wrong lens. "
            "Reject the blank to reset the job."
        )
        perm.unlock_steps = [
            f"Go to Production → {eye} Eye → '↩️ Reject & Return Blank to Stock'",
            "This resets the job to JOB_CREATED and restores inventory",
            "Then change power and re-enter the job card",
        ]

    elif max_depth == 3:
        perm.can_edit_power   = False
        perm.can_edit_product = False
        perm.required_role    = None
        perm.blocking_message = (
            f"⚠️ {eye} eye has a blank allotted "
            f"({'job card printed' if critical_line.job_stage == 'JOB_PRINTED' else 'blank picked from inventory'})."
        )
        perm.guidance = (
            "A blank has been allocated. "
            "Cancel the job card first so the blank returns to inventory, "
            "then change power."
        )
        perm.unlock_steps = [
            f"Go to Backoffice → this order → Documents tab → {eye} Eye Job Card",
            "Click '↩️ Reject & Return Blank to Stock' and select a reason",
            "Blank returns to stock and job resets to JOB_CREATED",
            "Then change power here and enter a new job card",
        ]

    elif max_depth == 1:
        # Job card created but no blank — safe with warning
        perm.guidance = (
            "⚠️ A job card exists but no blank has been picked yet. "
            "Changing power will update the job card — "
            "re-verify surfacing calculations after saving."
        )

    return perm


# ─────────────────────────────────────────────────────────────────────────────
# FIELD-LEVEL GUARD
# ─────────────────────────────────────────────────────────────────────────────

def field_is_editable(field_name: str, perm: EditPermission) -> bool:
    """True if field_name is editable given the EditPermission."""
    fn = field_name.lower()
    if fn in {"sph", "cyl", "axis", "add_power", "sphere", "cylinder"}:
        return perm.can_edit_power
    if fn in {"product_id", "product_name", "sku", "lens_params",
              "frame_group", "colour_mix", "batch_no", "category"}:
        return perm.can_edit_product
    if fn in {"quantity", "billing_qty"}:
        return perm.can_edit_qty
    if fn in {"unit_price", "discount_percent", "discount_amount",
              "gst_percent", "total_price"}:
        return perm.can_edit_price
    return True


# ─────────────────────────────────────────────────────────────────────────────
# BACKSTEP GOVERNANCE
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_backstep(job_id: str, from_stage: str, to_stage: str,
                      eye_side: str = "") -> BackstepResult:
    """
    Evaluate whether admin can move a job back from from_stage → to_stage.
    Returns BackstepResult with full cascade / consequence analysis.
    """
    result = BackstepResult(is_allowed=False)

    from_d = _STAGE_DEPTH.get(from_stage, 99)
    to_d   = _STAGE_DEPTH.get(to_stage,   99)

    if to_d >= from_d:
        result.blocking_reasons.append(
            f"{to_stage} is not earlier than {from_stage}."
        )
        return result

    allowed_target = BACKSTEP_ALLOWED_FROM.get(from_stage)
    if allowed_target != to_stage:
        result.blocking_reasons.append(
            f"Can only step back to '{allowed_target or 'none'}' from {from_stage}. "
            f"Cannot jump directly to {to_stage}. "
            f"Use consecutive backsteps."
        )
        return result

    result.is_allowed  = True
    result.requires_role = "admin" if from_stage in BACKSTEP_REQUIRES_ADMIN else "manager"
    eye = f" ({eye_side} eye)" if eye_side else ""

    # Per-stage cascade analysis
    if from_stage == "JOB_PRINTED":
        result.cascade_warnings = [f"Job card{eye} reset from JOB_PRINTED → JOB_CREATED"]
        result.auto_actions     = ["job_master.current_stage = JOB_CREATED", "backstep event logged"]

    elif from_stage == "PRODUCTION_PICKED":
        result.cascade_warnings = [
            f"⚠️ Blank inventory RESTORED (+1){eye} — blank physically returns to stock",
            "blank_allocations row deleted",
            "Job reset to JOB_PRINTED — blank must be re-selected",
        ]
        result.auto_actions = [
            "blank_inventory qty incremented by 1 (eye-specific if progressive/D-bifocal)",
            "blank_allocations row deleted",
            "job_master reset to JOB_PRINTED",
            "reprocess_count incremented",
        ]
        result.manual_steps = [
            "Verify blank is physically returned to its storage location",
            "Re-enter job card and select the correct blank",
        ]

    elif from_stage == "PRODUCTION_COMPLETED":
        result.cascade_warnings = [f"Job{eye} reverts to PRODUCTION_PICKED — surfacing output voided"]
        result.auto_actions     = ["job_master reset to PRODUCTION_PICKED"]
        result.manual_steps     = [
            "Physically return the half-processed lens to the surfacing department",
            "Re-do surfacing, then advance stage again",
        ]

    elif from_stage == "INSPECTION":
        result.cascade_warnings = [f"Inspection undone{eye} → back to PRODUCTION_COMPLETED"]
        result.auto_actions     = ["job_master reset to PRODUCTION_COMPLETED"]
        result.manual_steps     = ["Re-inspect the lens, then advance stage again"]

    elif from_stage in ("HARDCOAT_PICKED", "HARDCOAT_COMPLETED"):
        result.cascade_warnings = [
            f"⚠️ Hardcoat stage reversed{eye}",
            "Coating department must be notified immediately",
        ]
        result.auto_actions = ["job_master reset to INSPECTION"]
        result.manual_steps = [
            "Notify hardcoat department to stop processing",
            "If hardcoat already applied, a new blank may be required",
        ]

    elif from_stage in ("COLOURING_PICKED", "COLOURING_COMPLETED"):
        result.cascade_warnings = [
            f"⚠️ Colouring reversed{eye}",
            "Colour photo cleared from job record",
        ]
        result.auto_actions = [
            "job_master reset to INSPECTION",
            "colour_final_photo removed from lens_params",
        ]
        result.manual_steps = [
            "Notify colour department to hold / return lens",
            "If colour already applied, blank must be rejected and restarted",
        ]

    elif from_stage == "SENT_TO_ARC":
        result.cascade_warnings = [
            f"⚠️ ARC reversal{eye} — lens may be physically with ARC vendor",
            "System record only — does NOT recall the lens",
        ]
        result.auto_actions = ["job_master reset to INSPECTION"]
        result.manual_steps = [
            "Contact ARC vendor to confirm lens has NOT been processed yet",
            "Arrange physical return of lens from vendor",
            "If ARC already applied, a new blank + full rework is required",
        ]

    elif from_stage == "ARC_RECEIVED":
        result.cascade_warnings = [
            f"⚠️ ARC_RECEIVED reversed{eye}",
            "Record correction only — ARC coating is NOT physically undone",
        ]
        result.auto_actions = ["job_master reset to SENT_TO_ARC"]
        result.manual_steps = [
            "Use ONLY to correct a scan/entry error",
            "If ARC quality failed, use 'Reject & Return Blank' instead",
        ]

    elif from_stage in ("FINAL_QC", "READY_FOR_PACK"):
        result.cascade_warnings = [
            f"⚠️ Rolling back from {from_stage}{eye}",
            "Order may already be in the dispatch / billing queue",
        ]
        result.auto_actions = [f"job_master reset to {BACKSTEP_ALLOWED_FROM[from_stage]}"]
        result.manual_steps = [
            "Check if a billing invoice has been raised — reverse if needed",
            "Remove lens from dispatch tray",
            "Re-QC and advance stage again when ready",
        ]

    return result


def execute_backstep_db(job_id: str, from_stage: str, to_stage: str,
                         reason: str, order_id: str) -> tuple[bool, str]:
    """
    Execute the DB operations for a confirmed backstep.
    Returns (success: bool, message: str).
    Pure logic — no Streamlit calls.
    """
    try:
        from modules.sql_adapter import run_write, run_query
    except Exception as e:
        return False, f"DB unavailable: {e}"

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
            {"j": job_id, "ts": to_stage,
             "r": f"[BACKSTEP from {from_stage}] {reason}"}
        )

        # 3. Restore blank inventory when reversing PRODUCTION_PICKED
        if from_stage == "PRODUCTION_PICKED":
            rows = run_query(
                "SELECT ba.blank_id, ba.eye_side FROM blank_allocations ba "
                "JOIN job_master jm ON jm.order_line_id = ba.order_line_id "
                "WHERE jm.id = %(j)s::uuid LIMIT 1",
                {"j": job_id}
            )
            if rows:
                _bid = str(rows[0].get("blank_id") or "")
                _eye = str(rows[0].get("eye_side") or "")
                if _bid:
                    try:
                        from modules.sql_adapter import update_blank_quantity
                        update_blank_quantity(
                            _bid, qty_change=+1,
                            eye_side=_eye if _eye in ("R", "L") else None
                        )
                    except Exception:
                        pass
            run_write(
                "DELETE FROM blank_allocations ba "
                "USING job_master jm "
                "WHERE jm.order_line_id = ba.order_line_id "
                "  AND jm.id = %(j)s::uuid",
                {"j": job_id}
            )
            # Increment reprocess_count
            run_write(
                "UPDATE job_master SET reprocess_count = COALESCE(reprocess_count,0)+1 "
                "WHERE id = %(j)s::uuid",
                {"j": job_id}
            )

        # 4. Clear colour photo when reversing colouring stages
        if from_stage in ("COLOURING_COMPLETED", "COLOURING_PICKED"):
            try:
                run_write(
                    """UPDATE order_lines SET
                       lens_params = (COALESCE(lens_params,'{}')::jsonb
                                      - 'colour_final_photo'),
                       updated_at = NOW()
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
                               "reason": reason},
                      source="admin")
        except Exception:
            pass

        return True, f"Backstep {from_stage} → {to_stage} complete. Reason: {reason}"

    except Exception as e:
        return False, f"Backstep failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# UI BADGE HELPER
# ─────────────────────────────────────────────────────────────────────────────

DEPTH_LABELS = {
    0: ("🟢 No job card",        "#10b981"),
    1: ("🟡 Job card only",      "#f59e0b"),
    3: ("🟠 Blank allotted",     "#f97316"),
    4: ("🔴 In surfacing",       "#ef4444"),
    5: ("🔴 In coating/ARC",     "#dc2626"),
    6: ("🔒 PO in transit",      "#7c3aed"),
    7: ("🔒 Purchase received",  "#6b21a8"),
}

def depth_badge(depth: int) -> tuple[str, str]:
    for d in sorted(DEPTH_LABELS.keys(), reverse=True):
        if depth >= d:
            return DEPTH_LABELS[d]
    return ("🟢 Open", "#10b981")


__all__ = [
    "get_order_edit_permission",
    "field_is_editable",
    "evaluate_backstep",
    "execute_backstep_db",
    "EditPermission",
    "BackstepResult",
    "LineDepth",
    "depth_badge",
    "BACKSTEP_ALLOWED_FROM",
    "BACKSTEP_REQUIRES_ADMIN",
    "JOB_STAGE_ORDER",
    "PO_LOCKED_STATUSES",
]
