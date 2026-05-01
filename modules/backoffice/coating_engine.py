"""
Coating Path Engine
===================
Determines the production coating path for a job based on:
  1. Product name keywords  (HC, ARC, Photo, Tint, UC, etc.)
  2. Blank material         (from surfacing_data.blank_material)

Coating Paths
-------------
  UNCOATED          → PRODUCTION_DONE → INSPECTION → READY_FOR_PACK
  HARDCOAT          → PRODUCTION_DONE → INSPECTION → HARDCOAT_PICKED
                       → HARDCOAT_DONE → INSPECTION → READY_FOR_PACK
  COLOURING         → PRODUCTION_DONE → INSPECTION → COLOURING_PICKED
                       → COLOURING_DONE → INSPECTION → READY_FOR_PACK
  COLOURING_HC      → ... → COLOURING_DONE → HARDCOAT_PICKED
                       → HARDCOAT_DONE → INSPECTION → READY_FOR_PACK
  ARC               → PRODUCTION_DONE → INSPECTION → ARC_SENT
                       → ARC_RECEIVED → FINAL_QC → READY_FOR_PACK
  HARDCOAT_ARC      → ... → HARDCOAT_DONE → ARC_SENT
                       → ARC_RECEIVED → FINAL_QC → READY_FOR_PACK

Stage Sequences per path (after PRODUCTION_DONE)
------------------------------------------------------
Used to:
  - Filter allowed next-stage buttons in production panel
  - Determine READY_FOR_PACK condition per job
"""

from typing import Optional

# ── Keyword maps ──────────────────────────────────────────────────────────────

_PRODUCT_HC_KEYWORDS   = {"hc", "ultraHC", "hardcoat", "hard coat", "h/c", "ultra hc"}
_PRODUCT_ARC_KEYWORDS  = {"arc", "ar coat", "ar coating", "anti reflection", "antiref"}
_PRODUCT_COL_KEYWORDS  = {"photo", "photosun", "tint", "tinted", "colour", "color",
                           "photochromic", "graduated", "gradient"}
_PRODUCT_UC_KEYWORDS   = {"uc", "uncoated", "white", "clear basic"}

_BLANK_HC_KEYWORDS     = {"hc", "hardcoat", "ultraHC", "ultra hc"}
_BLANK_ARC_KEYWORDS    = {"arc", "ar"}
_BLANK_COL_KEYWORDS    = {"photo", "tint", "colour", "photosun", "sun", "brown", "grey",
                           "gray", "green"}


def detect_coating_path(
    product_name: str,
    blank_material: str = "",
    coating_type: str = "",
) -> str:
    """
    Returns one of:
      UNCOATED | HARDCOAT | COLOURING | COLOURING_HC | ARC | HARDCOAT_ARC

    Logic (both product + blank checked, union of signals):
      - ARC signal       → includes ARC in path
      - HC signal        → includes HARDCOAT in path
      - Colour signal    → includes COLOURING in path
      - No signals       → UNCOATED
    """
    pn  = (product_name  or "").lower()
    bm  = (blank_material or "").lower()
    ct  = (coating_type   or "").lower()

    # Combine all text signals
    combined = f"{pn} {bm} {ct}"

    has_arc   = any(k.lower() in combined for k in _PRODUCT_ARC_KEYWORDS)
    has_hc    = any(k.lower() in combined for k in _PRODUCT_HC_KEYWORDS)
    has_col   = any(k.lower() in combined for k in _PRODUCT_COL_KEYWORDS)

    # Explicit uncoated overrides everything
    is_uc = any(k.lower() in combined for k in _PRODUCT_UC_KEYWORDS)
    if is_uc and not has_hc and not has_arc:
        return "UNCOATED"

    if has_arc and has_hc:
        return "HARDCOAT_ARC"
    if has_arc:
        return "ARC"
    if has_col and has_hc:
        return "COLOURING_HC"
    if has_col:
        return "COLOURING"
    if has_hc:
        return "HARDCOAT"

    return "UNCOATED"


# ── Stage sequences per coating path ─────────────────────────────────────────
# Stages AFTER PRODUCTION_DONE in order. READY_FOR_PACK always last.

COATING_STAGE_SEQUENCES = {
    "UNCOATED": [
        "INSPECTION",
        "READY_FOR_PACK",
    ],
    "HARDCOAT": [
        "INSPECTION",
        "HARDCOAT_PICKED",
        "HARDCOAT_DONE",
        "INSPECTION",
        "READY_FOR_PACK",
    ],
    "COLOURING": [
        "INSPECTION",
        "COLOURING_PICKED",
        "COLOURING_DONE",
        "INSPECTION",
        "READY_FOR_PACK",
    ],
    "COLOURING_HC": [
        "INSPECTION",
        "COLOURING_PICKED",
        "COLOURING_DONE",
        "HARDCOAT_PICKED",
        "HARDCOAT_DONE",
        "INSPECTION",
        "READY_FOR_PACK",
    ],
    "ARC": [
        "INSPECTION",
        "ARC_SENT",
        "ARC_RECEIVED",
        "FINAL_QC",
        "READY_FOR_PACK",
    ],
    "HARDCOAT_ARC": [
        "INSPECTION",
        "HARDCOAT_PICKED",
        "HARDCOAT_DONE",
        "ARC_SENT",
        "ARC_RECEIVED",
        "FINAL_QC",
        "READY_FOR_PACK",
    ],
}

# Full sequence including pre-production stages
FULL_STAGE_SEQUENCE_PREFIX = [
    "JOB_CREATED",
    "PRINTED",
    "PRODUCTION_PICKED",
    "PRODUCTION_DONE",
]

# Fitting overlay — appended to any path when fitting service is present
# Fitting overlay stages (appended after first READY_FOR_PACK when fitting service present)
# FITTING_DONE is the inspection/acceptance checkpoint before dispatch
FITTING_STAGE_SEQUENCE = [
    "FITTING_PENDING",    # gate: operator selects fitter
    "FITTING_SENT",
    "FITTING_RECEIVED",
    "FITTING_DONE",       # = fitting inspection done → fitting ready (type 5)
]

# Ready type labels per coating path + fitting
READY_TYPE_LABELS = {
    "UNCOATED":     ("🟢", "UC Ready",      "#14532d", "#4ade80"),
    "COLOURING":    ("🟡", "Colour Ready",  "#713f12", "#fde047"),
    "COLOURING_HC": ("🔵", "HC Ready",      "#1e3a5f", "#60a5fa"),
    "HARDCOAT":     ("🔵", "HC Ready",      "#1e3a5f", "#60a5fa"),
    "ARC":          ("🔴", "ARC Ready",     "#450a0a", "#f87171"),
    "HARDCOAT_ARC": ("🔴", "ARC Ready",     "#450a0a", "#f87171"),
    "FITTING":      ("🟣", "Fitting Ready", "#2e1065", "#c084fc"),
}


def get_allowed_next_stages(
    current_stage: str,
    coating_path: str,
    has_fitting: bool = False,
) -> list:
    """
    Returns the next allowed stage(s) for a given coating path.

    has_fitting=True inserts the FITTING overlay after READY_FOR_PACK
    (first occurrence) so the lens goes to fitting before final dispatch.

    Handles INSPECTION appearing twice in some paths by using the last occurrence.
    """
    suffix = COATING_STAGE_SEQUENCES.get(coating_path, [])
    full   = FULL_STAGE_SEQUENCE_PREFIX + list(suffix)

    # If fitting service present, append fitting chain AFTER READY_FOR_PACK
    # (READY_FOR_PACK stays as the "lens ready" checkpoint; fitting is post-ready)
    if has_fitting and "READY_FOR_PACK" in full:
        rp_idx = full.index("READY_FOR_PACK")
        full   = full[:rp_idx + 1] + FITTING_STAGE_SEQUENCE

    # Find all positions of current_stage
    positions = [i for i, s in enumerate(full) if s == current_stage]
    if not positions:
        return []

    # Use last occurrence (handles repeated INSPECTION)
    idx = positions[-1] if len(positions) > 1 else positions[0]

    if idx + 1 < len(full):
        return [full[idx + 1]]
    return []


def get_ready_type(coating_path: str, has_fitting: bool = False) -> tuple:
    """
    Returns (emoji, label, bg_color, text_color) for the ready badge.
    """
    if has_fitting:
        return READY_TYPE_LABELS["FITTING"]
    return READY_TYPE_LABELS.get(coating_path, ("🟢", "Ready", "#14532d", "#4ade80"))


def is_job_complete(current_stage: str) -> bool:
    """True when job has reached READY_FOR_PACK."""
    return current_stage in ("READY_FOR_PACK", "DISPATCHED", "DELIVERED")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_coating_path_for_job(order_line_id: str) -> str:
    """
    Fetch coating path for a job from DB.
    Reads product name + coating_type from products table,
    blank_material from lens_params.surfacing_data.
    Returns coating path string.
    """
    try:
        from modules.sql_adapter import run_query
        import json

        rows = run_query("""
            SELECT
                p.product_name,
                p.coating_type,
                ol.lens_params
            FROM order_lines ol
            LEFT JOIN products p ON p.id = ol.product_id
            WHERE ol.id = %(lid)s::uuid
            LIMIT 1
        """, {"lid": order_line_id})

        if not rows:
            return "UNCOATED"

        r            = rows[0]
        product_name = r.get("product_name") or ""
        coating_type = r.get("coating_type") or ""
        lens_params  = r.get("lens_params") or {}

        if isinstance(lens_params, str):
            try:
                lens_params = json.loads(lens_params)
            except Exception:
                lens_params = {}

        surf         = lens_params.get("surfacing_data") or {}
        blank_mat    = surf.get("blank_material") or ""

        return detect_coating_path(product_name, blank_mat, coating_type)

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"get_coating_path_for_job failed: {e}")
        return "UNCOATED"


def save_coating_path_to_job(order_line_id: str, coating_path: str) -> None:
    """
    Persist coating_path into job_master for use in production panel.
    Requires: ALTER TABLE job_master ADD COLUMN IF NOT EXISTS coating_path VARCHAR(30);
    """
    try:
        from modules.sql_adapter import run_write
        run_write(
            "UPDATE job_master SET coating_path = %(cp)s, updated_at = NOW() "
            "WHERE order_line_id = %(lid)s::uuid",
            {"cp": coating_path, "lid": order_line_id}
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"save_coating_path_to_job failed: {e}")


def check_and_auto_ready_order(order_id: str) -> bool:
    """
    Check if ALL jobs for this order have reached READY_FOR_PACK (is_closed=TRUE).
    If yes → update orders.status = 'READY' automatically.
    Returns True if order was updated to READY.
    """
    try:
        from modules.sql_adapter import run_query, run_write

        # Count total vs closed jobs for this order
        rows = run_query("""
            SELECT
                COUNT(jm.id)                                    AS total,
                SUM(CASE WHEN jm.is_closed THEN 1 ELSE 0 END)  AS closed
            FROM job_master jm
            JOIN order_lines ol ON ol.id = jm.order_line_id
            WHERE ol.order_id = %(oid)s::uuid
              AND COALESCE(ol.is_deleted, FALSE) = FALSE
        """, {"oid": order_id})

        if not rows:
            return False

        total  = int(rows[0].get("total") or 0)
        closed = int(rows[0].get("closed") or 0)

        if total == 0 or closed < total:
            return False

        # All closed — auto-set READY
        # Use run_query with RETURNING to detect if row was actually updated
        updated_rows = run_query(
            "UPDATE orders SET status = 'READY', updated_at = NOW() "
            "WHERE id = %(oid)s::uuid "
            "  AND status NOT IN ('READY','BILLED','DISPATCHED','DELIVERED','CANCELLED') "
            "RETURNING id, status",
            {"oid": order_id}
        )
        actually_updated = bool(updated_rows)

        if actually_updated:
            # Log the auto-transition
            try:
                # Get previous status for history
                prev_rows = run_query(
                    "SELECT from_status FROM order_status_history "
                    "WHERE order_id = %(oid)s::uuid ORDER BY changed_at DESC LIMIT 1",
                    {"oid": order_id}
                )
                prev = prev_rows[0]["from_status"] if prev_rows else "IN_PRODUCTION"
                run_write("""
                    INSERT INTO order_status_history
                        (history_id, order_id, from_status, to_status,
                         changed_by_name, changed_at, remarks)
                    VALUES
                        (gen_random_uuid(), %(oid)s::uuid, %(prev)s, 'READY',
                         'production_engine', NOW(), 'Auto: all jobs completed')
                """, {"oid": order_id, "prev": prev})
            except Exception:
                pass

        return actually_updated

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"check_and_auto_ready_order failed: {e}")
        return False
