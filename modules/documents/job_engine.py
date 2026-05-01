"""
Job Card Engine - Generates job card data for in-house manufacturing
Enhanced with surfacing calculations
"""

from typing import Dict, List


# ==================================================
# GENERATE JOB CARD DATA
# ==================================================

def generate_job_card_data(order: dict) -> List[Dict]:
    """
    Generate job card data for all in-house lines.
    Includes surfacing data if available.
    """

    job_cards = []

    inhouse_lines = order.get("inhouse_lines", [])

    for line in inhouse_lines:

        # Extract surfacing data if exists
        surf_data = line.get("surfacing_data", {})

        # Extract lens params
        params = line.get("lens_params", {}) or {}

        card = {

            # -----------------------------
            # Order Info
            # -----------------------------

            "order_id": order.get("order_id"),
            "order_no": order.get("order_no"),
            "patient": order.get("patient_name"),
            "created_at": order.get("created_at"),

            # -----------------------------
            # Product Info
            # -----------------------------

            "product": line.get("product_name"),
            "brand": line.get("brand"),
            "category": line.get("category") or line.get("main_group"),
            "eye": line.get("eye_side"),

            # -----------------------------
            # Original RX
            # -----------------------------

            "sph": line.get("sph"),
            "cyl": line.get("cyl"),
            "axis": line.get("axis"),
            "add": line.get("add_power"),

            # -----------------------------
            # Manufacturing Power
            # -----------------------------

            "sph_out": line.get("sph_out"),
            "cyl_out": line.get("cyl_out"),
            "axis_out": line.get("axis_out"),

            # -----------------------------
            # Lens & Boxing Parameters
            # -----------------------------

            "lens_params": line.get("lens_params", {}),
            "boxing_params": line.get("boxing_params", {}),

            # Legacy extracted fields
            "lens_type": params.get("lens_type"),
            "diameter": params.get("diameter"),
            "frame_type": params.get("frame_type"),
            "fitting_height": params.get("fitting_height"),
            "base_curve": params.get("base_curve"),

            # -----------------------------
            # Quantity
            # -----------------------------

            "qty": line.get("billing_qty"),

            # -----------------------------
            # Surfacing Data (Optional)
            # -----------------------------

            "surfacing": {

                "blank_id": surf_data.get("blank_id"),
                "blank_brand": surf_data.get("blank_brand"),
                "blank_material": surf_data.get("blank_material"),
                "blank_batch": surf_data.get("blank_batch"),
                "base_curve": surf_data.get("base_curve"),

                "sph_surf": surf_data.get("sph_surf"),
                "cyl_surf": surf_data.get("cyl_surf"),
                "axis_surf": surf_data.get("axis_surf"),

                "tool_a": surf_data.get("tool_a"),
                "tool_b": surf_data.get("tool_b"),

                "kryptok_applied": surf_data.get("kryptok_applied", False),

                # ── Cost fields (internal only — not printed on job card) ──
                "blank_cost_per_pcs": float(surf_data.get("blank_cost_per_pcs") or 0),
                "blank_add": float(surf_data.get("blank_add") or 0),

            } if surf_data else None,

            # ── Cost summary (pcs basis — UI converts to pairs) ──────────
            "blank_cost_per_pcs": float(surf_data.get("blank_cost_per_pcs") or 0) if surf_data else 0,
        }

        job_cards.append(card)

    return job_cards


# ==================================================
# FORMAT JOB CARD FOR PRINTING
# ==================================================

def format_job_card_for_print(job_card: Dict) -> str:
    """
    Format a job card as plain text for printing.
    """

    lines = []

    lines.append("=" * 60)
    lines.append("JOB CARD - SURFACING OPERATION")
    lines.append("=" * 60)
    lines.append("")

    # -----------------------------
    # Header
    # -----------------------------

    lines.append(f"Order No: {job_card.get('order_no', 'N/A')}")
    lines.append(f"Patient: {job_card.get('patient', 'N/A')}")
    lines.append(f"Eye: {job_card.get('eye', 'N/A')}")
    lines.append("")

    # -----------------------------
    # Product
    # -----------------------------

    lines.append(f"Product: {job_card.get('product', 'N/A')}")
    lines.append(f"Brand: {job_card.get('brand', 'N/A')}")
    lines.append(f"Category: {job_card.get('category', 'N/A')}")
    lines.append("")

    # -----------------------------
    # Original RX
    # -----------------------------

    lines.append("-" * 60)
    lines.append("ORIGINAL PRESCRIPTION")
    lines.append("-" * 60)

    sph = job_card.get("sph") or 0
    cyl = job_card.get("cyl") or 0
    axis = job_card.get("axis") or 0
    add = job_card.get("add")

    lines.append(f"SPH: {sph:+.2f}")
    lines.append(f"CYL: {cyl:+.2f}")
    lines.append(f"AXIS: {axis}°")

    if add is not None:
        lines.append(f"ADD: {add:+.2f}")

    lines.append("")

    # -----------------------------
    # Surfacing Data
    # -----------------------------

    surf = job_card.get("surfacing")

    if surf:

        lines.append("-" * 60)
        lines.append("SURFACING POWERS (Minus Cylinder)")
        lines.append("-" * 60)

        lines.append(f"SPH: {surf.get('sph_surf'):+.2f}")
        lines.append(f"CYL: {surf.get('cyl_surf'):+.2f}")
        lines.append(f"AXIS: {surf.get('axis_surf')}°")

        if surf.get("kryptok_applied"):
            lines.append("")
            lines.append("⚠️ KRYPTOK AXIS CORRECTION APPLIED")

        lines.append("")

        lines.append("-" * 60)
        lines.append("BLANK & TOOLS")
        lines.append("-" * 60)

        lines.append(f"Blank Brand: {surf.get('blank_brand', 'N/A')}")
        lines.append(f"Blank Material: {surf.get('blank_material', 'N/A')}")
        lines.append(f"Batch No: {surf.get('blank_batch', 'N/A')}")
        lines.append("")

        base = surf.get("base_curve")

        if base is not None:
            lines.append(f"Base Curve: {base:.2f}D")

        lines.append(f"TOOL A: {surf.get('tool_a')}")
        lines.append(f"TOOL B: {surf.get('tool_b')}")
        lines.append("")

    # -----------------------------
    # QC Section
    # -----------------------------

    lines.append("-" * 60)
    lines.append("QUALITY CONTROL")
    lines.append("-" * 60)

    lines.append("□ Blank verified")
    lines.append("□ Tools set correctly")
    lines.append("□ Axis verified")
    lines.append("□ Power checked")
    lines.append("□ Final inspection")
    lines.append("")

    lines.append("-" * 60)
    lines.append("Technician: ________________   Date: ________________")
    lines.append("")

    lines.append("=" * 60)

    return "\n".join(lines)


# ==================================================
# COST & PROFIT HELPERS
# ==================================================

def get_blank_cost_for_line(job_card: Dict) -> float:
    """
    Return blank cost for one lens (1 pcs).
    Multiply by 2 for pair cost at UI level.
    """
    surf = job_card.get("surfacing") or {}
    return float(surf.get("blank_cost_per_pcs") or
                 job_card.get("blank_cost_per_pcs") or 0)


def get_pair_blank_cost(r_card: Dict, l_card: Dict) -> float:
    """
    Total blank cost for a pair (R + L pcs).
    """
    return get_blank_cost_for_line(r_card) + get_blank_cost_for_line(l_card)


def calculate_job_margin(job_card: Dict, selling_price_per_pcs: float = 0) -> Dict:
    """
    Simple margin calc per lens (pcs basis).
    All values in ₹ per pcs.

    Returns:
        {
          selling_price: float,
          blank_cost:    float,
          gross_margin:  float,
          margin_pct:    float,
        }
    """
    blank_cost = get_blank_cost_for_line(job_card)
    gross      = selling_price_per_pcs - blank_cost

    return {
        "selling_price": selling_price_per_pcs,
        "blank_cost":    blank_cost,
        "gross_margin":  gross,
        "margin_pct":    round((gross / selling_price_per_pcs * 100), 1)
                         if selling_price_per_pcs > 0 else 0,
    }
