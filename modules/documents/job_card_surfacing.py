"""
Job Card Generator for Surfacing Operations - ENHANCED VERSION
Flow: Category → Material → Add → Brand → Qty → Selection → Base Curve → Parameters → Calculations
"""

import streamlit as st
import logging
from typing import Dict, List, Optional
import pandas as pd

from modules.power_engine import (
    calculate_surfacing_powers,
    recommend_base_curve,
)

from modules.sql_adapter import (
    read_blank_inventory,
    get_blank_base_curves,
    update_blank_quantity,
)

log = logging.getLogger(__name__)


# ======================================================
# HELPER FUNCTIONS
# ======================================================

# ==================================================
# BASE CURVE HELPER
# ==================================================


# ======================================================
# STABLE KEY & IN-PROGRESS PERSISTENCE
# ======================================================

def _line_key(line: dict) -> str:
    """
    Build a STABLE session-state key for a line that survives page
    reloads, tab switches, and order re-fetches from DB.

    Priority:
      1. line_id (UUID) + eye_side  — most stable, preferred path
      2. id (DB PK)    + eye_side  — covers orders that pre-date line_id column
      3. order_no      + eye_side  — older orders with no UUID at all
      4. object id fallback        — last resort for old orders where eye_side
                                     is also absent; id(line) is NOT stable
                                     across page reloads but IS unique within
                                     a single Streamlit render cycle, which is
                                     all widget-key uniqueness requires.

    NOTE: eye_side is always included when present so R and L never share a key.
    """
    lid  = (line.get("line_id") or line.get("id") or "").strip()
    eye  = (line.get("eye_side") or "").upper().strip()
    ono  = (line.get("order_no") or "").strip()

    if lid and eye:
        return f"jc_{lid}_{eye}"
    if lid:
        # eye_side absent (old order) — lid is still unique per DB row
        return f"jc_{lid}"
    if ono and eye:
        return f"jc_{ono}_{eye}"
    # Absolute last resort: use Python object id as render-cycle discriminator.
    # Two dict objects for R and L will always sit at different memory addresses
    # within one render cycle, guaranteeing widget-key uniqueness even when all
    # identifying fields are absent.
    return f"jc_r{id(line)}"


def _pair_key(line: dict) -> str:
    """
    Shared key for R+L pair — same product, same order, NO eye side.
    Used only where pair grouping is intentional.
    """
    pid = (line.get("product_id") or line.get("product_name") or "unk").strip()
    ono = (line.get("order_no") or "").strip()
    return f"jc_pair_{pid}_{ono}"


def _eye_code(line: dict, default: str = "X") -> str:
    eye = str(line.get("eye_side") or "").upper().strip()[:1]
    return eye if eye in ("R", "L") else default


def _blank_selection_key(line: dict, eye: str | None = None) -> str:
    """Per-eye blank selection key. R and L must never overwrite each other."""
    eye_code = str(eye or _eye_code(line, "")).upper().strip()[:1]
    if eye_code in ("R", "L"):
        return f"selected_blank_{_pair_key(line)}_{eye_code}"
    return f"selected_blank_{_line_key(line)}"


def _paired_blank_selection_key(line: dict) -> tuple[str, str]:
    eye = _eye_code(line, "")
    other = "L" if eye == "R" else ("R" if eye == "L" else "")
    return (_blank_selection_key(line, other), other) if other else ("", "")


def _base_selection_key(line: dict, eye: str | None = None) -> str:
    """Per-eye final base key. Copy action can mirror it explicitly."""
    eye_code = str(eye or _eye_code(line, "")).upper().strip()[:1]
    if eye_code in ("R", "L"):
        return f"jc_base_final_{_pair_key(line)}_{eye_code}"
    return f"jc_base_final_{_line_key(line)}"


def _blank_eye_qty(blank: dict, eye_side: str, is_eye_specific: bool) -> tuple[int, str]:
    """Return available qty for the eye this job is saving."""
    if not is_eye_specific:
        return int(blank.get("qty_independent") or 0), "independent"
    eye = str(eye_side or "").upper()[:1]
    if eye == "L":
        return int(blank.get("qty_left") or 0), "L"
    return int(blank.get("qty_right") or 0), "R"


def _save_jc_progress(line: dict, state: dict) -> None:
    """
    Persist in-progress job card selections into order_lines.lens_params
    so they survive navigation / page reload.

    Stored under key "job_card_wip" inside the existing JSONB lens_params
    column — no schema changes needed.

    state keys saved:
        blank_id, blank_brand, blank_material, blank_batch,
    blank_colour, selected_material, selected_add,
        selected_brand, base_curve, base_input_mode
    """
    try:
        import json as _json
        from modules.sql_adapter import run_write, execute_query

        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return   # can't save without a line UUID

        # Fetch current lens_params to merge (don't overwrite other fields)
        rows = execute_query(
            "SELECT lens_params FROM order_lines WHERE id = %(lid)s::uuid LIMIT 1",
            "jc_wip_fetch", params={"lid": lid}
        )
        existing = {}
        if rows is not None and not rows.empty:
            lp = rows.iloc[0].get("lens_params") or {}
            if isinstance(lp, str):
                try:
                    lp = _json.loads(lp)
                except Exception:
                    lp = {}
            existing = lp if isinstance(lp, dict) else {}

        existing["job_card_wip"] = state
        run_write(
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb "
            "WHERE id = %(lid)s::uuid",
            {"lp": _json.dumps(existing), "lid": lid}
        )
    except Exception as _e:
        pass   # persistence is best-effort; UI still works from session state


def _load_jc_progress(line: dict) -> dict:
    """
    Load previously saved in-progress job card state from lens_params.
    Returns the wip dict, or {} if nothing was saved.
    """
    try:
        import json as _json
        from modules.sql_adapter import execute_query

        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return {}

        rows = execute_query(
            "SELECT lens_params FROM order_lines WHERE id = %(lid)s::uuid LIMIT 1",
            "jc_wip_load", params={"lid": lid}
        )
        if rows is None or rows.empty:
            return {}

        lp = rows.iloc[0].get("lens_params") or {}
        if isinstance(lp, str):
            try:
                lp = _json.loads(lp)
            except Exception:
                return {}
        if isinstance(lp, dict):
            return lp.get("job_card_wip") or {}
    except Exception:
        pass
    return {}


def _clear_jc_progress(line: dict) -> None:
    """Remove ONLY the job_card_wip key — never touch surfacing_data."""
    try:
        import json as _json
        from modules.sql_adapter import run_write, execute_query

        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return

        rows = execute_query(
            "SELECT lens_params FROM order_lines WHERE id = %(lid)s::uuid LIMIT 1",
            "jc_wip_clear_fetch", params={"lid": lid}
        )
        existing = {}
        if rows is not None and not rows.empty:
            lp = rows.iloc[0].get("lens_params") or {}
            if isinstance(lp, str):
                try:
                    lp = _json.loads(lp)
                except Exception:
                    lp = {}
            existing = lp if isinstance(lp, dict) else {}

        # SAFETY: only remove wip key — preserve surfacing_data and everything else
        if "job_card_wip" not in existing:
            return  # Nothing to clear, skip DB write entirely

        existing.pop("job_card_wip", None)
        run_write(
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb "
            "WHERE id = %(lid)s::uuid",
            {"lp": _json.dumps(existing), "lid": lid}
        )
    except Exception:
        pass


def _persist_surfacing_to_db(line: dict) -> None:
    """
    Write surfacing_data into order_lines.lens_params under key 'surfacing_data'.
    Also upserts blank_allocations so advance_job_stage(PRODUCTION_PICKED) can
    find the blank_id — the DB function checks blank_allocations.blank_id.
    Called after a successful blank quantity update so the job card is durable in DB.
    """
    try:
        import json as _json
        from modules.sql_adapter import run_write, execute_query

        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return

        # Fetch current lens_params
        rows = execute_query(
            "SELECT lens_params FROM order_lines WHERE id = %(lid)s::uuid LIMIT 1",
            "jc_persist_fetch", params={"lid": lid}
        )
        existing = {}
        if rows is not None and not rows.empty:
            lp = rows.iloc[0].get("lens_params") or {}
            if isinstance(lp, str):
                try:
                    lp = _json.loads(lp)
                except Exception:
                    lp = {}
            existing = lp if isinstance(lp, dict) else {}

        # Embed surfacing_data
        existing["surfacing_data"] = line.get("surfacing_data", {})
        # Remove WIP now that final data is saved
        existing.pop("job_card_wip", None)

        run_write(
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb "
            "WHERE id = %(lid)s::uuid",
            {"lp": _json.dumps(existing), "lid": lid}
        )

        # ── Write blank_allocations ──────────────────────────────────────────
        # advance_job_stage(PRODUCTION_PICKED) queries blank_allocations for
        # blank_id. Without this row the function returns "ERROR: Blank not selected".
        surf = line.get("surfacing_data") or {}
        blank_id = surf.get("blank_id") or ""
        eye_side = (line.get("eye_side") or "")[:1].upper()
        base_sel = surf.get("base_curve")

        if blank_id and lid:
            run_write(
                """
                INSERT INTO blank_allocations
                    (id, order_line_id, blank_id, eye_side, base_selected, allocated_at)
                VALUES
                    (gen_random_uuid(), %(lid)s::uuid, %(bid)s::uuid,
                     %(eye)s, %(base)s, NOW())
                ON CONFLICT (order_line_id) DO UPDATE
                    SET blank_id      = EXCLUDED.blank_id,
                        eye_side      = EXCLUDED.eye_side,
                        base_selected = EXCLUDED.base_selected,
                        allocated_at  = NOW()
                """,
                {
                    "lid":  lid,
                    "bid":  blank_id,
                    "eye":  eye_side or None,
                    "base": float(base_sel) if base_sel else None,
                }
            )

    except Exception as _e:
        log.warning("_persist_surfacing_to_db failed: %s", _e)


def _upsert_job_master(line: dict, order: dict) -> None:
    """
    Create a job_master row for this order_line if one doesn't exist yet.
    Uses INSERT ... ON CONFLICT (order_line_id) DO NOTHING — requires the
    UNIQUE constraint on job_master.order_line_id (see fix_job_master_duplicates.sql).

    blank_allocated_qty is set to total_qty (billing_qty) — not hardcoded 1 —
    so advance_job_stage() PRODUCTION_PICKED check passes for qty > 1 orders.
    """
    try:
        from modules.sql_adapter import run_write

        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return

        qty = int(line.get("billing_qty") or line.get("quantity") or 1)

        # Single INSERT with ON CONFLICT — no separate SELECT needed.
        # The UNIQUE constraint on order_line_id ensures only one row ever exists.
        run_write(
            """
            INSERT INTO job_master
                (id, order_line_id, total_qty, blank_required_qty,
                 blank_allocated_qty, current_stage, reprocess_count, is_closed,
                 created_at, updated_at)
            VALUES
                (gen_random_uuid(), %(lid)s::uuid, %(qty)s, %(qty)s,
                 %(qty)s, 'JOB_CREATED', 0, false,
                 NOW(), NOW())
            ON CONFLICT (order_line_id) DO UPDATE
                SET blank_allocated_qty = EXCLUDED.blank_allocated_qty,
                    total_qty           = EXCLUDED.total_qty,
                    blank_required_qty  = EXCLUDED.blank_required_qty,
                    updated_at          = NOW()
                WHERE job_master.current_stage = 'JOB_CREATED'
            """,
            {"lid": lid, "qty": qty}
        )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning(f"_upsert_job_master failed: {_e}")


def get_base_curve_options(blank: dict, system_base: float):
    """
    Build base curve options from:
    - DB base_recommended
    - base_1 / base_2 / base_3
    - system calculated base

    Returns list of:
    {
        value: float,
        label: str,
        is_recommended: bool
    }
    """

    options = []
    seen = set()

    def add_base(val, tag="", is_rec=False):

        if val is None:
            return

        try:
            v = round(float(val), 2)
        except (TypeError, ValueError):
            return

        if v in seen:
            return

        seen.add(v)

        label = f"{v:.2f}D"

        if tag:
            label += f" {tag}"

        options.append({
            "value": v,
            "label": label,
            "is_recommended": is_rec
        })


    # -----------------------------
    # From Database
    # -----------------------------

    db_rec = blank.get("base_recommended")
    b1 = blank.get("base_1")
    b2 = blank.get("base_2")
    b3 = blank.get("base_3")

    add_base(db_rec, "⭐ DB", True)
    add_base(b1)
    add_base(b2)
    add_base(b3)


    # -----------------------------
    # System Calculated
    # -----------------------------

    if system_base:
        add_base(system_base, "⚙️ SYS", False)


    # -----------------------------
    # Sort
    # -----------------------------

    options = sorted(options, key=lambda x: x["value"])


    # -----------------------------
    # Ensure One Recommended
    # -----------------------------

    if not any(o["is_recommended"] for o in options) and options:

        # Mark closest to system base
        closest = min(
            options,
            key=lambda x: abs(x["value"] - system_base)
        )

        closest["is_recommended"] = True
        closest["label"] += " ⭐"


    return options


def get_available_materials(category: str) -> List[str]:
    """Get unique materials for a category from blank inventory"""
    blanks = read_blank_inventory(category=category, active_only=True)
    if blanks.empty:
        return []
    return sorted(blanks["material"].dropna().unique().tolist())


def get_add_power_range(base_add: float) -> List[float]:
    """
    Generate add power range: base ± 0.25
    Example: If base is 2.50, returns [2.25, 2.50, 2.75]
    """
    if base_add is None:
        return []
    
    add_values = [
        round(base_add - 0.25, 2),
        round(base_add, 2),
        round(base_add + 0.25, 2)
    ]
    
    # Filter out negative values
    return [a for a in add_values if a >= 0]


def get_available_brands(category: str, material: str, add_power: Optional[float]) -> List[str]:
    """Get brands filtered by category, material, and add power"""
    blanks = read_blank_inventory(category=category, material=material, active_only=True)
    
    if blanks.empty:
        return []
    
    # Filter by add power if specified
    if add_power is not None:
        blanks = blanks[
            blanks["add_power"].isna() |
            (blanks["add_power"] == float(add_power))
        ]
    
    if blanks.empty:
        return []
    
    return sorted(blanks["brand"].dropna().unique().tolist())


def get_brands_with_stock(
    category: str,
    material: str,
    add_power: Optional[float],
    eye_side: str = "",
) -> List[dict]:
    """
    Return brands with aggregated stock summary for display in dropdown.
    Each entry: {brand, total_r, total_l, total_ind, bases, label}
    label = "GKB — R:12 L:8 | Base: 4.0D 6.0D"
    """
    blanks = read_blank_inventory(category=category, material=material, active_only=True)
    if blanks.empty:
        return []

    if add_power is not None:
        blanks = blanks[
            blanks["add_power"].isna() |
            (blanks["add_power"] == float(add_power))
        ]
    if blanks.empty:
        return []

    is_eye_specific = any(x in category.lower()
                          for x in ["progressive", "d bifocal", "v2", "pal", "bifocal"])

    result = []
    for brand in sorted(blanks["brand"].dropna().unique().tolist()):
        bdf = blanks[blanks["brand"] == brand]
        total_r   = int(bdf["qty_right"].fillna(0).sum())       if "qty_right"       in bdf.columns else 0
        total_l   = int(bdf["qty_left"].fillna(0).sum())        if "qty_left"        in bdf.columns else 0
        total_ind = int(bdf["qty_independent"].fillna(0).sum()) if "qty_independent" in bdf.columns else 0

        bases = []
        if "base_recommended" in bdf.columns:
            bases = sorted(set(
                round(float(v), 2)
                for v in bdf["base_recommended"].dropna()
                if float(v) > 0
            ))

        # Build label — show per-base breakdown in dropdown
        if is_eye_specific:
            active_qty = total_r if eye_side.upper() == "R" else total_l
            # Build per-base R/L summary for label
            base_parts = []
            for b in bases:
                b_rows = bdf[bdf["base_recommended"].apply(
                    lambda v: abs(float(v) - b) < 0.05 if pd.notna(v) else False
                )] if "base_recommended" in bdf.columns else pd.DataFrame()
                br = int(b_rows["qty_right"].fillna(0).sum()) if not b_rows.empty else 0
                bl = int(b_rows["qty_left"].fillna(0).sum())  if not b_rows.empty else 0
                base_parts.append(f"Base {b:.0f}: R-{br} L-{bl}")
            stock_str = f"R:{total_r}  L:{total_l}"
            warn = " ⚠" if active_qty == 0 else (" !" if active_qty < 3 else "")
        else:
            stock_str = f"Qty:{total_ind}"
            base_parts = []
            for b in bases:
                b_rows = bdf[bdf["base_recommended"].apply(
                    lambda v: abs(float(v) - b) < 0.05 if pd.notna(v) else False
                )] if "base_recommended" in bdf.columns else pd.DataFrame()
                bi = int(b_rows["qty_independent"].fillna(0).sum()) if not b_rows.empty else 0
                base_parts.append(f"Base {b:.0f}: {bi}")
            warn = " ⚠" if total_ind == 0 else (" !" if total_ind < 3 else "")

        base_str = ("  |  " + "  ".join(base_parts)) if base_parts else ""
        label = f"{brand} — {stock_str}{base_str}{warn}"

        result.append({
            "brand":     brand,
            "total_r":   total_r,
            "total_l":   total_l,
            "total_ind": total_ind,
            "bases":     bases,
            "label":     label,
            "has_stock": (total_r > 0 or total_l > 0 or total_ind > 0),
        })

    # Sort: brands with stock first, then alphabetical
    result.sort(key=lambda x: (not x["has_stock"], x["brand"]))
    return result


def get_available_colours(
    category: str,
    material: str,
    add_power: Optional[float],
    brand: str,
) -> List[str]:
    """Get distinct colour values for category+material+add+brand combo.
    Returns list; empty string '' means 'Clear / no tint' rows."""
    blanks = read_blank_inventory(category=category, material=material,
                                   brand=brand, active_only=True)
    if blanks.empty:
        return []
    if add_power is not None:
        blanks = blanks[
            blanks["add_power"].isna() |
            (blanks["add_power"] == float(add_power))
        ]
    if blanks.empty:
        return []
    colours = blanks["colour"].fillna("").unique().tolist()
    # Sort: non-empty first (alphabetically), then empty ("Clear/no tint") last
    named   = sorted([c for c in colours if c.strip()])
    unnamed = [c for c in colours if not c.strip()]
    return named + ([""] if unnamed else [])


def filter_blanks_by_selection(
    category: str,
    material: str,
    add_power: Optional[float],
    brand: str,
    colour: str,
    eye_side: str,
) -> pd.DataFrame:
    """
    Get blanks filtered by all selections with quantity > 0
    """
    blanks = read_blank_inventory(
        category=category,
        material=material,
        brand=brand,
        active_only=True
    )
    
    if blanks.empty:
        return pd.DataFrame()
    
    # Filter by add power
    if add_power is not None:
        blanks = blanks[
            blanks["add_power"].isna() |
            (blanks["add_power"] == float(add_power))
        ]

    # Filter by colour (empty string = rows where colour IS NULL or empty)
    if colour != "":
        blanks = blanks[blanks["colour"].fillna("").str.lower() == colour.lower()]
    else:
        blanks = blanks[blanks["colour"].fillna("") == ""]

    # Filter by quantity
    is_eye_specific = any(x in category.lower() for x in ["progressive", "d bifocal", "v2", "pal"])
    
    def qty_ok(row):
        if is_eye_specific:
            if eye_side == "R":
                return row["qty_right"] > 0
            else:
                return row["qty_left"] > 0
        return row["qty_independent"] > 0
    
    blanks = blanks[blanks.apply(qty_ok, axis=1)]
    
    return blanks


def read_blanks_for_stock_display(
    category: str,
    material: str,
    add_power: Optional[float],
    brand: str,
    colour: str,
) -> pd.DataFrame:
    """
    Same filters as filter_blanks_by_selection but WITHOUT qty > 0 restriction.
    Used purely for displaying R/L stock per base in dropdowns — we want to show
    Base 4: R-0 L-4 even when R is zero, so the user knows not to assign R from it.
    """
    blanks = read_blank_inventory(
        category=category,
        material=material,
        brand=brand,
        active_only=True
    )
    if blanks.empty:
        return pd.DataFrame()

    if add_power is not None:
        blanks = blanks[
            blanks["add_power"].isna() |
            (blanks["add_power"] == float(add_power))
        ]

    if colour != "":
        blanks = blanks[blanks["colour"].fillna("").str.lower() == colour.lower()]
    else:
        blanks = blanks[blanks["colour"].fillna("") == ""]

    return blanks

# ======================================================
# MAIN UI - ENHANCED FLOW
# ======================================================

def render_surfacing_job_card(line: Dict, order: Dict, show_buttons: bool = True):
    """
    Enhanced surfacing job card with:
    1. Category (locked)
    2. Material, Add, Brand selection
    3. Qty display with highlighting
    4. Blank selection
    5. Base curve dropdown (recommended + base_1, base_2, base_3)
    6. Lens parameters (diameter, frame type)
    7. Calculations & tool display
    """
    
    st.markdown("### 🔧 Surfacing Job Card")

    # ── PRODUCTION STAGE LOCK ─────────────────────────────────────
    # If job_master stage is past JOB_CREATED, the job is in production.
    # Block all edits — show read-only summary only.
    try:
        from modules.sql_adapter import run_query as _rq2
        _lid2 = (line.get("line_id") or line.get("id") or "").strip()
        _jm_stage = None
        if _lid2:
            _jm_rows = _rq2(
                "SELECT current_stage FROM job_master WHERE order_line_id = %(l)s::uuid LIMIT 1",
                {"l": _lid2}
            )
            if _jm_rows:
                _jm_stage = _jm_rows[0].get("current_stage")
    except Exception:
        _jm_stage = None

    _LOCKED_STAGES = {"JOB_PRINTED", "PRODUCTION_PICKED", "PRODUCTION_COMPLETED",
                      "INSPECTION", "HARDCOAT_PICKED", "HARDCOAT_COMPLETED",
                      "COLOURING_PICKED", "COLOURING_COMPLETED", "SENT_TO_ARC",
                      "ARC_RECEIVED", "FINAL_QC", "READY_FOR_PACK", "DISPATCHED"}

    if _jm_stage and _jm_stage in _LOCKED_STAGES:
        surf = line.get("surfacing_data") or {}
        st.markdown(
            f"<div style='background:#1e3a5f;border:1px solid #3b82f6;border-radius:6px;"
            f"padding:8px 14px;margin-bottom:10px'>"
            f"<span style='color:#60a5fa;font-weight:700'>🔒 Job in Production — Stage: {_jm_stage}</span>"
            f"<span style='color:#93c5fd;font-size:0.8rem;margin-left:8px'>"
            f"Job card is locked. Changes not allowed once production has started.</span>"
            f"</div>",
            unsafe_allow_html=True
        )
        if surf:
            c1, c2, c3 = st.columns(3)
            c1.metric("Blank", f"{surf.get('blank_brand','—')} {surf.get('blank_material','—')}")
            c2.metric("Base Curve", f"{float(surf.get('base_curve') or 0):.2f}D")
            c3.metric("Colour", surf.get('blank_colour') or 'Clear')
            import math as _mm
            def _sf(v): 
                try: f=float(v or 0); return 0.0 if _mm.isnan(f) else f
                except (TypeError, ValueError): return 0.0
            st.info(
                f"**RX:** SPH {_sf(line.get('sph')):+.2f} / CYL {_sf(line.get('cyl')):+.2f} / AXIS {int(_sf(line.get('axis')))}°  |  "
                f"**SURF:** SPH {_sf(surf.get('sph_surf')):+.2f} / CYL {_sf(surf.get('cyl_surf')):+.2f}  |  "
                f"Dia: {surf.get('diameter','—')}  |  Frame: {surf.get('frame_type','—')}"
            )
        return  # ← nothing else rendered

    # ── In-progress status banner ──────────────────────────────
    _lk_banner = _line_key(line)
    _blank_ss_key_banner = _blank_selection_key(line)
    _surf_done = bool(line.get("surfacing_data"))
    _blank_picked = _blank_ss_key_banner in st.session_state
    if _surf_done:
        st.success("✅ Job card saved — surfacing data recorded.")
    elif _blank_picked:
        st.warning(
            "⚠️ **Job card in progress — blank selected but not yet saved.** "
            "Complete and click '💾 Save Job Card' before leaving this order."
        )
    else:
        st.info("📋 Select a blank below to begin the job card.")


    # --------------------------------------------------
    # NaN-SAFE HELPERS  (float NaN is truthy → `val or 0` returns NaN, not 0)
    # --------------------------------------------------
    import math as _math

    def _safe_float(v, default=0.0) -> float:
        """Return float(v) if v is a real number, else default."""
        if v is None:
            return default
        try:
            f = float(v)
            return default if _math.isnan(f) or _math.isinf(f) else f
        except (TypeError, ValueError):
            return default

    def _safe_int(v, default=0) -> int:
        """Return int(v) if v is a real number, else default."""
        f = _safe_float(v, float(default))
        return int(f)

    def _safe_add(v) -> Optional[float]:
        """Return cleaned add_power, or None if absent/NaN/zero."""
        if v is None:
            return None
        try:
            f = float(v)
            if _math.isnan(f) or _math.isinf(f):
                return None
            return f if f != 0.0 else None
        except (TypeError, ValueError):
            return None

    # --------------------------------------------------
    # RX PRESCRIPTION
    # --------------------------------------------------
    sph      = _safe_float(line.get("sph"))
    cyl      = _safe_float(line.get("cyl"))
    axis     = _safe_int(line.get("axis"))
    add_power = _safe_add(line.get("add_power"))
    eye_side = (line.get("eye_side") or "").upper().strip()
    
    # --------------------------------------------------
    # CATEGORY (LOCKED - from product master)
    # --------------------------------------------------
    # Resolution priority (most specific → most generic):
    #   1. category column  e.g. "KT bifocals", "Single Vision", "Progressive"
    #   2. lens_category    e.g. "KT bifocals", "SV", "PAL"  (may be empty)
    #   3. main_group       e.g. "Ophthalmic Lenses"          (generic fallback)
    #
    # The product table has:
    #   main_group   = "Ophthalmic Lenses"   ← too generic, avoid as primary
    #   category     = "KT bifocals"          ← USE THIS FIRST
    #   lens_category = ""                    ← optional extra label
    #
    # NEVER use main_group as the blank-query category unless both
    # category and lens_category are empty.

    # "type" is the user-facing label (Excel column "Type" → DB column "category")
    # Try both line.get("type") and line.get("category") — same DB value, two aliases
    _type_col  = (line.get("type")         or "").strip()
    _cat_col   = (line.get("category")     or "").strip()
    lens_category_raw = (line.get("lens_category") or "").strip()
    _main_grp  = (line.get("main_group")   or "").strip()

    # Prefer "type" (the product master field name users know), fall back to "category"
    _cat_col = _type_col or _cat_col

    # Pick most specific non-generic value
    # Treat "Ophthalmic Lenses" / "Ophthalmic Lens" as generic (skip them)
    _GENERIC = {"OPHTHALMIC LENSES", "OPHTHALMIC LENS", ""}
    raw_category = (
        _cat_col          if _cat_col.upper()   not in _GENERIC else
        lens_category_raw if lens_category_raw              else
        _main_grp
    )
    if not raw_category:
        raw_category = _cat_col or _main_grp

    # If still generic, do a direct DB lookup on product master
    _product_id = str(line.get("product_id") or "").strip()
    if raw_category.upper() in _GENERIC and _product_id:
        try:
            from modules.sql_adapter import run_query as _rq_cat2
            _cat_rows2 = _rq_cat2(
                "SELECT COALESCE(category,'') AS cat, "
                "COALESCE(lens_category,'') AS lcat, "
                "COALESCE(product_name,'') AS pname "
                "FROM products WHERE id = %(pid)s::uuid LIMIT 1",
                {"pid": _product_id}
            )
            if _cat_rows2:
                _db_cat2  = str(_cat_rows2[0].get("cat")  or "").strip()
                _db_lcat2 = str(_cat_rows2[0].get("lcat") or "").strip()
                _db_name2 = str(_cat_rows2[0].get("pname") or "").strip()
                if _db_cat2 and _db_cat2.upper() not in _GENERIC:
                    raw_category = _db_cat2
                    _cat_col     = _db_cat2
                if not lens_category_raw:
                    lens_category_raw = _db_lcat2
                if not _prod_name_lower and _db_name2:
                    _prod_name_lower = _db_name2.lower()
        except Exception:
            pass

    # ── KT/Kryptok fallback: detect from product name if category doesn't say so ──
    # Products like "UV Photosun Brown KT 1.56 UltraHC" have "KT" in the name
    # but the category column may say "Single Vision" (incorrectly entered in product master).
    # If the product name or lens_category contains a KT keyword, override the raw_category
    # so the correct blank category AND axis correction are applied.
    _prod_name_lower = (line.get("product_name") or "").lower()
    _lc_lower        = lens_category_raw.lower()
    _raw_lower       = raw_category.lower()
    _KT_KEYWORDS = ("kt bifocal", "kt bifocals", " kt ", "kryptok", "/kt/", "-kt-", "(kt)")
    _name_has_kt = any(k in f" {_prod_name_lower} " for k in _KT_KEYWORDS) or _prod_name_lower.endswith(" kt")
    _lc_has_kt   = any(k in f" {_lc_lower} " for k in _KT_KEYWORDS) or _lc_lower.strip() in ("kt", "kt bifocal", "kt bifocals", "kryptok")
    _cat_not_kt  = "kryptok" not in _raw_lower and "kt" not in _raw_lower
    if (_name_has_kt or _lc_has_kt) and _cat_not_kt:
        raw_category = "KT bifocals"   # will map to "Kryptok" in _CATEGORY_MAP below

    # Map product master values → blank_inventory category values.
    # blank_inventory.category uses: "Single Vision", "Progressive", "Bifocal",
    #   "Kryptok", "D Bifocal", "Toric", "Reading"  (actual DB values).
    _CATEGORY_MAP = {
        # ── Single Vision ────────────────────────────────────────────
        "SINGLE VISION":        "Single Vision",
        "SINGLE_VISION":        "Single Vision",   # DB normalisation variant
        "SINGLE VISION LENSES": "Single Vision",
        "SV":                   "Single Vision",
        "SV LENSES":            "Single Vision",

        # ── Progressive ──────────────────────────────────────────────
        "PROGRESSIVE":          "Progressive",
        "PAL":                  "Progressive",
        "PROGRESSIVE LENSES":   "Progressive",

        # ── Bifocal (flat-top / executive) ───────────────────────────
        "BIFOCAL":              "Bifocal",
        "BIFOCALS":             "Bifocal",
        "BF":                   "Bifocal",

        # ── Kryptok / KT Bifocal ─────────────────────────────────────
        # KT (Kryptok) are round-seg bifocals.
        # blank_inventory stores these under category = "Kryptok" — match exactly.
        "KT BIFOCAL":           "Kryptok",
        "KT BIFOCALS":          "Kryptok",
        "KRYPTOK":              "Kryptok",
        "KRYPTOK BIFOCAL":      "Kryptok",
        "KRYPTOK BIFOCALS":     "Kryptok",
        "KT":                   "Kryptok",

        # ── D-Bifocal (eye-specific) ──────────────────────────────────
        "D BIFOCAL":            "D Bifocal",
        "D BIFOCALS":           "D Bifocal",
        "D-BIFOCAL":            "D Bifocal",

        # ── Toric ────────────────────────────────────────────────────
        "TORIC":                "Toric",

        # ── Reading ──────────────────────────────────────────────────
        "READING":              "Reading",
        "READERS":              "Reading",

        # ── Legacy fallback: generic ophthalmic → DO NOT default to Progressive ──
        # These are resolved LAST; if lens_category is set it takes priority above.
        "OPHTHALMIC LENSES":    "Single Vision",   # generic → SV (safer default)
        "OPHTHALMIC LENS":      "Single Vision",
    }
    category = _CATEGORY_MAP.get(raw_category.upper(), raw_category).strip()

    # D-Bifocal blanks are tracked per eye (qty_right / qty_left).
    # Progressive blanks are also eye-specific.
    is_eye_specific = any(x in category.lower() for x in ["progressive", "d bifocal"])
    
    # ==================================================
    # WIP RESTORE — reload in-progress selections if user navigated away
    # ==================================================
    _lk = _line_key(line)
    _wip_ss_key = f"jc_wip_loaded_{_lk}"

    # On first render of this line in this session, pull saved state from DB
    if not st.session_state.get(_wip_ss_key):
        _wip = _load_jc_progress(line)
        if _wip:
            # Restore blank selection into session state
            _blank_ss_key = _blank_selection_key(line)
            if _wip.get("blank_row") and _blank_ss_key not in st.session_state:
                st.session_state[_blank_ss_key] = _wip["blank_row"]
        st.session_state[_wip_ss_key] = True   # mark as loaded (even if nothing to restore)

    # ==================================================
    # DISPLAY: ORIGINAL RX
    # ==================================================
    st.markdown("#### 📋 Original Prescription")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("SPH", f"{sph:+.2f}")
    c2.metric("CYL", f"{cyl:+.2f}")
    c3.metric("AXIS", f"{axis}°")
    if add_power is not None:
        c4.metric("ADD", f"{float(add_power):+.2f}")
    
    st.markdown("---")
    
    # ==================================================
    # STEP 1: CATEGORY (LOCKED - DISPLAY ONLY)
    # ==================================================
    st.markdown("#### 📦 Blank Selection")
    
    # Show both the mapped blank category AND the original product lens_category
    _display_cat = category
    if lens_category_raw and lens_category_raw.upper() != category.upper():
        _display_cat = f"{category}  ·  🏷 {lens_category_raw}"
    st.info(f"**Type:** {_display_cat}")
    
    # Check if any blanks exist for this category
    all_category_blanks = read_blank_inventory(category=category, active_only=True)
    
    if all_category_blanks.empty:
        st.error("❌ No blanks available for this category")
        return
    
    # ==================================================
    # STEP 2: MATERIAL DROPDOWN
    # ==================================================
    available_materials = get_available_materials(category)
    
    if not available_materials:
        st.error("❌ No materials available for this category")
        return
    
    # Auto-detect material from product/treatment — user can always change
    def _auto_material(line_data: dict, mats: list) -> int:
        _lp_mat = line_data.get("lens_params") or {}
        if isinstance(_lp_mat, str):
            try:
                import json as _mat_json
                _lp_mat = _mat_json.loads(_lp_mat)
            except Exception:
                _lp_mat = {}
        _lp_mat = _lp_mat if isinstance(_lp_mat, dict) else {}
        p = " ".join(str(x or "") for x in (
            line_data.get("product_name"),
            line_data.get("material"),
            line_data.get("category"),
            line_data.get("brand"),
            _lp_mat.get("material"),
            _lp_mat.get("lens_material"),
            _lp_mat.get("treatment"),
            _lp_mat.get("coating"),
            _lp_mat.get("display_product_name"),
            _lp_mat.get("display_suffix"),
        )).upper()
        def _idx(kw):
            for i, m in enumerate(mats):
                if kw.upper() in str(m).upper():
                    return i
            return None
        def _idx_any(*kws):
            for kw in kws:
                r = _idx(kw)
                if r is not None:
                    return r
            return None
        if any(x in p for x in ("BLUECUT", "BLUE CUT", "BLUE-CUT", "BLUE BLOCK", "BLUEBLOCK", "BLU CUT", "BLUCUT")):
            r = _idx_any("Bluecut", "Blue Cut", "Blue", "BLC", "UV420")
            if r is not None:
                return r
        if any(x in p for x in ("CLEAR","UV CLEAR","UNCOAT","UC KT","UV KT","KT CLEAR")):
            r = _idx("Clear");  return r if r is not None else 0
        if any(x in p for x in ("PHOTO","PHOTOCHRO","PC KT","PHOTO KT","CHROMATIC")):
            r = _idx("Photo");  return r if r is not None else (_idx("Photochro") or 0)
        if any(x in p for x in ("BIFOCAL","KT BI","KRYPTOK","FLAT TOP","ROUND TOP","RIBBON")):
            r = _idx("Bifocal"); return r if r is not None else 0
        if any(x in p for x in ("TINT","SUNGLASS","SOLAR")):
            r = _idx("Tint");   return r if r is not None else 0
        if any(x in p for x in ("PROGRESSIVE","PROG","PAL")):
            r = _idx("Progressive"); return r if r is not None else (_idx("Clear") or 0)
        return 0

    _mat_default = _auto_material(line, available_materials)

    selected_material = st.selectbox(
        "Material",
        available_materials,
        index=_mat_default,
        key=f"material_{_line_key(line)}"
    )
    
    # ==================================================
    # STEP 3: ADD POWER DROPDOWN (product line ± 0.25)
    # ==================================================
    # Initialise before the conditional so it is always defined.
    # Single Vision lenses have no add_power — without this initialisation
    # the call to get_available_brands() below would crash with NameError.
    selected_add: Optional[float] = None

    if add_power is not None:
        add_range = get_add_power_range(float(add_power))
        
        # Format options with indication of product line value
        add_options = []
        for a in add_range:
            if a == float(add_power):
                add_options.append(f"{a:.2f} (Product Line)")
            else:
                add_options.append(f"{a:.2f}")
        
        selected_add_display = st.selectbox(
            "Add Power",
            add_options,
            index=add_range.index(float(add_power)) if float(add_power) in add_range else 0,
            key=f"add_{_line_key(line)}"
        )
        
        # Extract numeric value
        if selected_add_display:
            selected_add = float(str(selected_add_display).split()[0])
        else:
            selected_add = float(add_power)
    else:
        # Single Vision — no add power widget needed
        st.caption("ℹ️ No add power required for this product")
    
    # ==================================================
    # STEP 4: BRAND / SUPPLIER SELECTION  (with stock summary)
    # ==================================================
    _brand_data = get_brands_with_stock(category, selected_material, selected_add, eye_side)

    if not _brand_data:
        st.warning(f"⚠️ No brands available for {selected_material}"
                   + (f" with Add {selected_add:.2f}" if selected_add else ""))
        return

    # Separate brands with and without stock — always show all but highlight
    _brand_labels = [b["label"] for b in _brand_data]
    _brand_names  = [b["brand"] for b in _brand_data]

    # Try to preserve previous selection
    _prev_brand = st.session_state.get(f"brand_{_line_key(line)}")
    _brand_idx  = 0
    if _prev_brand and _prev_brand in _brand_names:
        _brand_idx = _brand_names.index(_prev_brand)

    selected_brand_label = st.selectbox(
        "Brand / Supplier  (R · L · Base shown)",
        _brand_labels,
        index=_brand_idx,
        key=f"brand_{_line_key(line)}",
        help="Stock levels shown per brand. ⚠ = zero stock, ! = below 3 pcs.",
    )
    selected_brand = _brand_names[_brand_labels.index(selected_brand_label)]
    _selected_brand_meta = _brand_data[_brand_names.index(selected_brand)]

    # Show a brief stock callout under the dropdown for the chosen brand
    _sb_r   = _selected_brand_meta["total_r"]
    _sb_l   = _selected_brand_meta["total_l"]
    _sb_ind = _selected_brand_meta["total_ind"]
    _sb_bases = _selected_brand_meta["bases"]
    if is_eye_specific:
        _active_qty = _sb_r if eye_side.upper() == "R" else _sb_l
        _stock_color = "#22c55e" if _active_qty >= 5 else ("#f59e0b" if _active_qty > 0 else "#ef4444")
        _bases_txt = "  ·  Base: " + ", ".join(f"{b:.0f}" for b in _sb_bases) if _sb_bases else ""
        st.markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;padding:2px 0 6px'>"
            f"<span style='color:{_stock_color}'>"
            f"{eye_side.upper()} Qty:{_active_qty} · R:{_sb_r} L:{_sb_l}</span>"
            f"{_bases_txt}"
            f"{'  <span style=\"color:#ef4444\">⚠ No stock for this eye</span>' if _active_qty == 0 else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        _stock_color = "#22c55e" if _sb_ind >= 5 else ("#f59e0b" if _sb_ind > 0 else "#ef4444")
        _bases_txt = "  ·  Base: " + ", ".join(f"{b:.0f}" for b in _sb_bases) if _sb_bases else ""
        st.markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;padding:2px 0 6px'>"
            f"<span style='color:{_stock_color}'>Qty:{_sb_ind}</span>"
            f"{_bases_txt}"
            f"{'  <span style=\"color:#ef4444\">⚠ No stock</span>' if _sb_ind == 0 else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ==================================================
    # STEP 4B: COLOUR FILTER
    # ==================================================
    available_colours = get_available_colours(category, selected_material, selected_add, selected_brand)

    # Always render as selectbox — even with one option — so the user sees
    # what colour is selected. Empty string = rows where colour IS NULL/empty.
    if available_colours:
        colour_labels = [c if c.strip() else "Clear / no tint" for c in available_colours]
        selected_colour_label = st.selectbox(
            "Colour",
            colour_labels,
            key=f"colour_{_line_key(line)}"
        )
        selected_colour = available_colours[colour_labels.index(selected_colour_label)]
    else:
        selected_colour = ""

    # ==================================================
    # STEP 4C: BASE CURVE PRE-FILTER  (with per-base R/L stock)
    # ==================================================

    # First: get ALL blanks for this selection (before base filter)
    _pre_filter_blanks = filter_blanks_by_selection(
        category=category,
        material=selected_material,
        add_power=selected_add,
        brand=selected_brand,
        colour=selected_colour,
        eye_side=eye_side
    )

    # For base stock display: fetch ALL rows without qty restriction so we can
    # show "Base 4: R-0 L-4" even when R has zero stock on that base.
    _stock_display_blanks = read_blanks_for_stock_display(
        category=category,
        material=selected_material,
        add_power=selected_add,
        brand=selected_brand,
        colour=selected_colour,
    )

    _available_bases = []
    _base_stock: dict = {}   # base_value → {r, l, ind}
    _src_df = _stock_display_blanks if not _stock_display_blanks.empty else _pre_filter_blanks
    if not _src_df.empty and "base_recommended" in _src_df.columns:
        for _, _brow in _src_df.iterrows():
            _bv = _brow.get("base_recommended")
            if _bv is not None and pd.notna(_bv) and float(_bv) > 0:
                _bk = round(float(_bv), 2)
                if _bk not in _base_stock:
                    _base_stock[_bk] = {"r": 0, "l": 0, "ind": 0}
                _base_stock[_bk]["r"]   += int(_brow.get("qty_right", 0)       or 0)
                _base_stock[_bk]["l"]   += int(_brow.get("qty_left", 0)        or 0)
                _base_stock[_bk]["ind"] += int(_brow.get("qty_independent", 0) or 0)
    _available_bases = sorted(_base_stock.keys())

    _base_pre_key = f"jc_base_pre_{_line_key(line)}"
    if len(_available_bases) > 1:
        # Build labels: "Base 4.0 : R-10  L-12" / "Base 6.0 : R-2  L-4"
        def _base_label(b):
            s = _base_stock.get(b, {})
            if is_eye_specific:
                _r = s.get("r", 0)
                _l = s.get("l", 0)
                _active = _r if eye_side.upper() == "R" else _l
                _warn = " ⚠" if _active == 0 else (" !" if _active < 3 else "")
                return f"Base {b:.0f} :  R-{_r}  L-{_l}{_warn}"
            else:
                _qi = s.get("ind", 0)
                _warn = " ⚠" if _qi == 0 else (" !" if _qi < 3 else "")
                return f"Base {b:.0f} :  Qty {_qi}{_warn}"

        _base_labels = [_base_label(b) for b in _available_bases]
        _prev_base_pre = st.session_state.get(_base_pre_key)
        _prev_idx = 0
        if _prev_base_pre and _prev_base_pre in _available_bases:
            _prev_idx = _available_bases.index(_prev_base_pre)
        _sel_base_label = st.selectbox(
            "Select Base",
            _base_labels,
            index=_prev_idx,
            key=f"jc_base_pre_sel_{_line_key(line)}",
            help="R and L stock shown per Base. ⚠ = zero stock for active eye, ! = below 3.",
        )
        _pre_selected_base = _available_bases[_base_labels.index(_sel_base_label)]
        st.session_state[_base_pre_key] = _pre_selected_base
    elif _available_bases:
        _pre_selected_base = _available_bases[0]
        st.session_state[_base_pre_key] = _pre_selected_base
        b = _pre_selected_base
        s = _base_stock.get(b, {})
        if is_eye_specific:
            st.caption(f"Base {b:.0f}  R-{s.get('r',0)}  L-{s.get('l',0)}")
        else:
            st.caption(f"Base {b:.0f}  Qty {s.get('ind',0)}")
    else:
        _pre_selected_base = None

    # ==================================================
    # STEP 5: GET FILTERED BLANKS (base-filtered)
    # ==================================================
    if not _pre_filter_blanks.empty and _pre_selected_base is not None:
        # Filter to the physical inventory base only. Base 1/2/3 are alternate
        # surfacing choices, not stock identity.
        _base_mask = pd.Series([False] * len(_pre_filter_blanks), index=_pre_filter_blanks.index)
        if "base_recommended" in _pre_filter_blanks.columns:
            _base_mask = (
                _pre_filter_blanks["base_recommended"].apply(
                    lambda v: abs(float(v) - _pre_selected_base) < 0.05 if pd.notna(v) else False
                )
            )
        filtered_blanks = _pre_filter_blanks[_base_mask]
    else:
        filtered_blanks = _pre_filter_blanks

    if filtered_blanks.empty:
        st.warning("⚠️ No blanks available with selected filters and sufficient quantity")
        return
    
    # ==================================================
    # STEP 6: DISPLAY AVAILABLE BLANKS WITH QTY
    # ==================================================

    # ── Barcode scanner — scan laminated card to auto-select blank ────────────
    _scan_col, _clear_col = st.columns([3, 1])
    with _scan_col:
        _scanned = st.text_input(
            "📷 Scan blank barcode",
            placeholder="Scan laminated blank card barcode to auto-select",
            key=f"blank_scan_{_line_key(line)}",   # unique per eye — prevents duplicate key crash
            label_visibility="collapsed",
        ).strip().upper()
        if _scanned:
            st.session_state[f"blank_scan_val_{_line_key(line)}"] = _scanned
    with _clear_col:
        if st.button("✕", key=f"blank_scan_clear_{_line_key(line)}", use_container_width=True,
                     help="Clear scanner input"):
            st.session_state.pop(f"blank_scan_val_{_line_key(line)}", None)
            st.session_state.pop(f"blank_scan_{_line_key(line)}", None)
            st.rerun()

    _scan_val = st.session_state.get(f"blank_scan_val_{_line_key(line)}", "")
    if _scan_val and not filtered_blanks.empty:
        # Try to match barcode against filtered blanks
        _bc_match = filtered_blanks[
            filtered_blanks["barcode"].astype(str).str.upper().str.strip() == _scan_val
        ]
        if _bc_match.empty:
            # Try item_code as fallback
            _bc_match = filtered_blanks[
                filtered_blanks.get("item_code", pd.Series()).astype(str).str.upper().str.strip() == _scan_val
            ] if "item_code" in filtered_blanks.columns else _bc_match
        if not _bc_match.empty:
            _sel_key = _blank_selection_key(line)
            _matched_blank = _bc_match.iloc[0].to_dict()
            st.session_state[_sel_key] = _matched_blank
            _save_jc_progress(line, {"blank_row": _matched_blank})
            st.success(
                f"✅ Auto-selected: **{_matched_blank['brand']}** {_matched_blank['material']} "
                f"| Batch {_matched_blank.get('batch_no','—')} "
                f"| R:{_matched_blank.get('qty_right',0)} L:{_matched_blank.get('qty_left',0)}"
            )
        else:
            st.warning(f"⚠️ Barcode **{_scan_val}** not found in available blanks for this prescription")

    st.markdown("#### 🗃️ Available Blanks")

    # Always show both R and L qty — blank selection is shared
    if is_eye_specific:
        active_col   = "qty_right"
        inactive_col = "qty_left"
    else:
        active_col   = "qty_independent"
        inactive_col = None

    for idx2, blank_row in filtered_blanks.iterrows():
        qty_active   = int(blank_row[active_col])   if pd.notna(blank_row.get(active_col))   else 0
        qty_inactive = int(blank_row[inactive_col]) if (inactive_col and pd.notna(blank_row.get(inactive_col))) else None
        base_rec     = blank_row.get("base_recommended")
        base_display = f"{float(base_rec):.1f}D" if base_rec and pd.notna(base_rec) else "—"

        checkbox_key = f"select_blank_{blank_row['id']}_{_pair_key(line)}_{_line_key(line)}"

        # Determine if already selected for this eye only.
        _sel_key = _blank_selection_key(line)
        is_selected = (
            _sel_key in st.session_state
            and str(st.session_state[_sel_key].get("id")) == str(blank_row["id"])
        )

        border_color = "#4ade80" if is_selected else "#334155"
        bg_color     = "#0d2818" if is_selected else "#111827"

        st.markdown(
            f"""<div style='border:1.5px solid {border_color};border-radius:8px;
            padding:0;margin-bottom:8px;background:{bg_color};overflow:hidden;'>
            </div>""",
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            col_info, col_qty_active, col_qty_other, col_base, col_check = st.columns([4, 1.5, 1.5, 1.5, 1.5])

            with col_info:
                st.markdown(
                    f"**{blank_row['brand']}** · {blank_row['material']} · "
                    f"<span style='color:#94a3b8'>{blank_row.get('colour','')}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(f"Batch {blank_row.get('batch_no') or '—'} · {blank_row.get('location') or '—'}")

            with col_qty_active:
                # For eye-specific (Progressive/D Bifocal): show qty_right
                # For independent (Kryptok/SV): show qty_independent
                if is_eye_specific:
                    qty_r = int(blank_row["qty_right"]) if pd.notna(blank_row.get("qty_right")) else 0
                    _r_label = "👁 R"
                else:
                    qty_r = int(blank_row.get("qty_independent", 0)) if pd.notna(blank_row.get("qty_independent")) else 0
                    _r_label = "📦 Qty"
                color_r = "#4ade80" if qty_r >= 5 else ("#f59e0b" if qty_r > 0 else "#ef4444")
                last_r  = "<div style='font-size:0.6rem;color:#f59e0b;font-weight:700'>⚠️ LAST 1</div>" if qty_r == 1 else ""
                st.markdown(
                    f"<div style='text-align:center;'>"
                    f"<div style='font-size:0.7rem;color:#94a3b8;'>{_r_label}</div>"
                    f"<div style='font-size:1.6rem;font-weight:700;color:{color_r};line-height:1.1'>{qty_r}</div>"
                    f"{last_r}</div>",
                    unsafe_allow_html=True,
                )

            with col_qty_other:
                qty_l = int(blank_row["qty_left"]) if (inactive_col and pd.notna(blank_row.get("qty_left"))) else (
                        int(blank_row.get("qty_independent", 0)) if pd.notna(blank_row.get("qty_independent")) else 0)
                color_l = "#4ade80" if qty_l >= 5 else ("#f59e0b" if qty_l > 0 else "#ef4444")
                last_l  = "<div style='font-size:0.6rem;color:#f59e0b;font-weight:700'>⚠️ LAST 1</div>" if qty_l == 1 else ""
                st.markdown(
                    f"<div style='text-align:center;'>"
                    f"<div style='font-size:0.7rem;color:#475569;'>👁 L</div>"
                    f"<div style='font-size:1.2rem;font-weight:500;color:{color_l};line-height:1.3'>{qty_l}</div>"
                    f"{last_l}</div>",
                    unsafe_allow_html=True,
                )

            with col_base:
                st.markdown(
                    f"<div style='text-align:center;'>"
                    f"<div style='font-size:0.7rem;color:#94a3b8;'>Base</div>"
                    f"<div style='font-size:1.1rem;font-weight:600;color:#e2e8f0;'>{base_display}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

            with col_check:
                if is_selected:
                    st.markdown(
                        "<div style='text-align:center;padding:8px 0'>"
                        "<span style='color:#4ade80;font-size:1.4rem'>✅</span>"
                        "<div style='color:#4ade80;font-size:0.65rem;font-weight:700'>SELECTED</div>"
                        "</div>",
                        unsafe_allow_html=True)
                else:
                    if st.button(
                        "Select",
                        key=checkbox_key,
                        type="primary",
                        use_container_width=True,
                    ):
                        _selected_blank_dict = blank_row.to_dict()
                        # Deselect any previously selected blank for this eye only.
                        st.session_state[_sel_key] = _selected_blank_dict
                        _save_jc_progress(line, {"blank_row": _selected_blank_dict})
                        # No st.rerun() — selection registers immediately via session_state

    
    # ==================================================
    # STEP 8: PROCESS SELECTED BLANK
    # ==================================================
    selected_blank_key = _blank_selection_key(line)

    
    if selected_blank_key not in st.session_state:
        st.info("👆 Please select a blank to continue")
        return
    
    blank = st.session_state[selected_blank_key]
    
    st.success(f"✅ Selected: {blank['brand']} {blank['material']} (Batch: {blank['batch_no']})")
    if is_eye_specific:
        _sel_qty, _sel_eye = _blank_eye_qty(blank, eye_side, is_eye_specific)
        _other_eye = "L" if str(eye_side or "").upper().startswith("R") else "R"
        _other_qty = int(blank.get("qty_left" if _other_eye == "L" else "qty_right") or 0)
        _qty_colour = "#22c55e" if _sel_qty > 0 else "#ef4444"
        st.markdown(
            f"<div style='font-size:0.78rem;color:#94a3b8;margin-top:-4px'>"
            f"{_sel_eye} blank selection · "
            f"<span style='color:{_qty_colour};font-weight:700'>{_sel_eye} available: {_sel_qty}</span>"
            f" · {_other_eye} available: {_other_qty}</div>",
            unsafe_allow_html=True,
        )
        _other_blank_key, _other_eye_for_copy = _paired_blank_selection_key(line)
        if st.button(
            f"↔ Copy this blank to {_other_eye_for_copy} eye",
            key=f"copy_pair_blank_{_pair_key(line)}_{_line_key(line)}",
            use_container_width=True,
            help="Copies supplier/blank/base to the other eye selection only. Save each eye separately; stock is checked per eye.",
        ):
            if _other_blank_key:
                st.session_state[_other_blank_key] = dict(blank)
                _this_base = st.session_state.get(_base_selection_key(line))
                if _this_base is not None:
                    st.session_state[_base_selection_key(line, _other_eye_for_copy)] = float(_this_base)
                st.success(f"Blank copied to {_other_eye_for_copy} eye selection. Open/save {_other_eye_for_copy} separately.")
            st.rerun()
    
    st.markdown("---")
    
    # ==================================================
    # SURFACING CALCULATIONS (WITHOUT BASE)
    # ==================================================
    # Pass lens_category_raw so Kryptok detection works
    # (category is now the blank-inventory mapped value e.g. "Bifocal";
    #  lens_category_raw still contains "KT bifocals" / "kryptok" etc.)
    surf_calc = calculate_surfacing_powers(
        sph=sph,
        cyl=cyl,
        axis=axis,
        eye_side=eye_side,
        category=_cat_col or lens_category_raw or category,   # KT/Kryptok detection
        base_curve=None
    )
    
    st.markdown("#### 🔨 Surfacing Powers (Minus Cyl)")
    c1, c2, c3 = st.columns(3)
    c1.metric("SPH", f"{surf_calc.get('sph_surf', 0):+.2f}")
    c2.metric("CYL", f"{surf_calc.get('cyl_surf', 0):+.2f}")
    
    axis_label = "AXIS"
    if surf_calc.get("kryptok_correction_applied"):
        axis_label += " ⚠️ KRYPTOK"
        st.warning(
            "🔄 **Kryptok / KT Bifocal detected** — Axis has been adjusted by ±15° "
            f"(Right eye: −15°, Left eye: +15°). Original axis: **{axis}°** → "
            f"Surfacing axis: **{int(surf_calc.get('axis_surf') or 0)}°**"
        )
    
    c3.metric(axis_label, f"{int(surf_calc.get('axis_surf') or 0)}°")
    
    st.markdown("---")
    
    # ==================================================
    # STEP 9: BASE CURVE DROPDOWN
    # (Recommended + base_1, base_2, base_3 from blank)
    # ==================================================

    st.markdown("#### 🎯 Base Curve Selection")

    # System calculated base is guidance only. Physical inventory identity is
    # the blank row's base_recommended.
    system_recommended_base = recommend_base_curve(
        surf_calc["sph_surf"],
        surf_calc["cyl_surf"]
    )
    try:
        inventory_base = round(float(blank.get("base_recommended")), 2)
    except Exception:
        inventory_base = None

    # If user pre-selected a base from the blank filter, use that as the recommended default
    _pre_base_from_filter = st.session_state.get(f"jc_base_pre_{_line_key(line)}")
    if _pre_base_from_filter and inventory_base is None:
        inventory_base = _pre_base_from_filter

    st.caption(
        f"💡 System calculated base: **{system_recommended_base:.2f}D**"
        + (f" · 📦 Inventory base: **{inventory_base:.2f}D**" if inventory_base else "")
    )

    # Build options from DB + System
    base_options = get_base_curve_options(blank, system_recommended_base)

    if not base_options:
        st.error("❌ No base curves available for this blank")
        return

    # Labels
    base_labels = [opt["label"] for opt in base_options]

    # Default = physical inventory base first; otherwise DB/system recommended
    default_idx = 0
    for i, opt in enumerate(base_options):
        if inventory_base is not None and abs(float(opt["value"]) - float(inventory_base)) < 0.05:
            default_idx = i
            break
        if opt["is_recommended"]:
            default_idx = i

    # --- Input mode toggle ---
    _base_mode_key = f"base_mode_{_line_key(line)}"
    base_input_mode = st.radio(
        "Base Curve Input",
        ["Dropdown", "Manual"],
        index=0,
        horizontal=True,
        key=_base_mode_key,
    )

    # ── Per-eye base state. Copy button mirrors this explicitly to the partner.
    _base_final_key = _base_selection_key(line)
    _saved_base = st.session_state.get(_base_final_key)
    if _saved_base is not None:
        for _pi_idx, _po in enumerate(base_options):
            if abs(float(_po["value"]) - float(_saved_base)) < 0.05:
                default_idx = _pi_idx
                break

    if base_input_mode == "Dropdown":
        selected_base_label = st.selectbox(
            "Select Base Curve",
            base_labels,
            index=default_idx,
            key=f"base_{_line_key(line)}"
        )
        selected_idx = base_labels.index(selected_base_label)
        base_curve = base_options[selected_idx]["value"]
    else:
        _manual_key = f"base_manual_{_line_key(line)}"
        manual_val = st.number_input(
            "Enter Base Curve (D)",
            min_value=0.25,
            max_value=20.0,
            value=float(_saved_base or inventory_base or system_recommended_base),
            step=0.25,
            format="%.2f",
            key=_manual_key,
        )
        base_curve = round(float(manual_val), 2)

    # Write this eye's selected base only to this eye.
    st.session_state[_base_final_key] = float(base_curve)

    if _saved_base is not None and abs(float(_saved_base) - float(base_curve)) < 0.05:
        st.caption("🔗 Base restored for this eye")

    st.success(
        f"✅ Selected Base Curve: **{base_curve:.2f}D**"
    )

    st.markdown("---")
    
    # ==================================================
    # STEP 10: LENS PARAMETERS — pre-filled from lens_params
    # Always read fresh from DB so backoffice/punching edits are reflected
    # even when the line dict passed in is a cached/stale object.
    # ==================================================
    import json as _lpj3

    def _fresh_lens_params(line: dict) -> dict:
        """Fetch the latest lens_params from DB for this line."""
        try:
            from modules.sql_adapter import run_query as _rq_lp
            _lid_lp = (line.get("line_id") or line.get("id") or "").strip()
            if not _lid_lp:
                raise ValueError("no lid")
            _rows_lp = _rq_lp(
                "SELECT lens_params FROM order_lines WHERE id = %(id)s::uuid LIMIT 1",
                {"id": _lid_lp}
            )
            if not _rows_lp:
                raise ValueError("not found")
            _lp_raw = _rows_lp[0].get("lens_params") or {}
            if isinstance(_lp_raw, str):
                try:
                    _lp_raw = _lpj3.loads(_lp_raw) if _lp_raw else {}
                except Exception:
                    _lp_raw = {}
            return _lp_raw if isinstance(_lp_raw, dict) else {}
        except Exception:
            # Fallback: use the dict already on the line object
            _fb = line.get("lens_params") or {}
            if isinstance(_fb, str):
                try:
                    _fb = _lpj3.loads(_fb)
                except Exception:
                    _fb = {}
            return _fb if isinstance(_fb, dict) else {}

    _lp3 = _fresh_lens_params(line)

    # ── Map lens_params frame_type → processing frame type ──────────────────
    _lp_frame   = str(_lp3.get("frame_type") or "").strip()
    _lp_thick   = str(_lp3.get("thickness") or "").strip()
    _lp_inst    = str(_lp3.get("instructions") or "").strip()
    _lp_diam    = str(_lp3.get("diameter") or "").strip()
    _lp_fit_ht  = str(_lp3.get("fitting_height") or "").strip()
    _lp_colour  = str(_lp3.get("colour") or "").strip()
    _lp_colour  = "" if _lp_colour.lower() in ("none","no","") else _lp_colour
    _lp_fit_req = bool(_lp3.get("fitting_required"))
    _lp_fit_type= str(_lp3.get("fitting_type") or "").strip()

    # Frame type mapping
    _FRAME_OPTIONS = ["Full Rim", "Semi Rimless", "Rimless", "Drill Mount", "Grooved"]
    _frame_map = {
        "full":      "Full Rim",
        "full rim":  "Full Rim",
        "supra":     "Semi Rimless",
        "rimless":   "Rimless",
        "three piece":"Drill Mount",
    }
    # If frame_type was set in punching/backoffice, map it; otherwise leave at index 0
    # (do NOT default to "Full Rim" when the field was intentionally left blank)
    _frame_prefill = _frame_map.get(_lp_frame.lower(), None) if _lp_frame else None
    if _frame_prefill not in _FRAME_OPTIONS:
        _frame_prefill = _FRAME_OPTIONS[0]  # index 0 = "Full Rim" as neutral first option
    _frame_idx = _FRAME_OPTIONS.index(_frame_prefill)

    # Diameter from lens_params or boxing_params
    _DIAM_OPTIONS = ["65mm", "70mm", "75mm", "80mm", "Custom"]
    _bp3 = line.get("boxing_params") or {}
    if isinstance(_bp3, str):
        try: _bp3 = _lpj3.loads(_bp3)
        except Exception as e:
            log.debug("Could not parse boxing_params: %s", e)
        _bp3 = {}
    _ed_raw = str(
        _lp_diam
        or _bp3.get("diameter")
        or _bp3.get("dia")
        or _bp3.get("ed")
        or ""
    ).strip()
    # Default to first option (65mm) when no diameter specified in order
    # Only snap to a specific size when punching/backoffice actually set a value
    _diam_prefill = None
    if _ed_raw:
        try:
            _ed_val = float(_ed_raw)
            for _do in ["65mm","70mm","75mm","80mm"]:
                if abs(float(_do.replace("mm","")) - _ed_val) < 3:
                    _diam_prefill = _do; break
            else:
                _diam_prefill = "Custom"
        except Exception:
            pass
    if _diam_prefill not in _DIAM_OPTIONS:
        _diam_prefill = _DIAM_OPTIONS[0]  # first option, not hardcoded "75mm"
    _diam_idx = _DIAM_OPTIONS.index(_diam_prefill)

    # Show lens_params badge strip if data present
    _lp_badges = []
    if _lp_frame:
        _lp_badges.append(f"<span style='background:#1e293b;color:#94a3b8;padding:3px 8px;border-radius:20px;font-size:0.68rem'>🖼 {_lp_frame}</span>")
    if _lp_thick and _lp_thick.lower() not in ("regular",""):
        _lp_badges.append(f"<span style='background:#1e293b;color:#94a3b8;padding:3px 8px;border-radius:20px;font-size:0.68rem'>📏 {_lp_thick}</span>")
    if _lp_colour:
        _lp_badges.append(f"<span style='background:#4a0526;color:#f9a8d4;border:1px solid #be185d;padding:3px 8px;border-radius:20px;font-size:0.68rem;font-weight:700'>🎨 {_lp_colour}</span>")
    if _lp_fit_req:
        _lp_badges.append(f"<span style='background:#2d1b69;color:#c4b5fd;border:1px solid #7c3aed;padding:3px 8px;border-radius:20px;font-size:0.68rem;font-weight:700'>🔧 {_lp_fit_type or 'Fitting'}</span>")
    if _lp_fit_ht:
        _lp_badges.append(f"<span style='background:#1e293b;color:#fbbf24;padding:3px 8px;border-radius:20px;font-size:0.68rem'>↕ FH {_lp_fit_ht}</span>")

    st.markdown("#### ⚙️ Lens Processing Parameters")

    if _lp_badges:
        st.markdown(
            "<div style='display:flex;flex-wrap:wrap;gap:5px;padding:4px 0 8px 0'>"
            "<span style='color:#60a5fa;font-size:0.7rem;font-weight:700;padding:3px 4px'>📋 From order:</span>"
            + "".join(_lp_badges) + "</div>",
            unsafe_allow_html=True)

    col_param1, col_param2 = st.columns(2)
    
    with col_param1:
        selected_diameter = st.selectbox(
            "Blank Diameter",
            _DIAM_OPTIONS,
            index=_diam_idx,
            key=f"diameter_{_line_key(line)}"
        )
        if selected_diameter == "Custom":
            _custom_dv = float(_ed_raw) if _ed_raw else 75.0
            custom_diameter = st.number_input(
                "Enter diameter (mm)", min_value=50.0, max_value=100.0,
                value=_custom_dv, step=0.5,
                key=f"custom_diameter_{_line_key(line)}"
            )
            final_diameter = f"{custom_diameter}mm"
        else:
            final_diameter = selected_diameter
    
    with col_param2:
        selected_frame_type = st.selectbox(
            "Frame Type",
            _FRAME_OPTIONS,
            index=_frame_idx,
            key=f"frame_type_{_line_key(line)}"
        )
    
    col_param3, col_param4 = st.columns(2)
    
    with col_param3:
        _EDGE_OPTIONS = ["Standard", "Thin Edge", "Rolled Edge", "Polished Edge"]
        _edge_map = {"thin":"Thin Edge", "cartier thick":"Rolled Edge"}
        _edge_prefill = _edge_map.get(_lp_thick.lower(), None) if _lp_thick else None
        if _edge_prefill not in _EDGE_OPTIONS:
            _edge_prefill = _EDGE_OPTIONS[0]  # neutral first option, not hardcoded "Standard"
        selected_edge_type = st.selectbox(
            "Edge Finish", _EDGE_OPTIONS,
            index=_EDGE_OPTIONS.index(_edge_prefill),
            key=f"edge_type_{_line_key(line)}"
        )
    
    with col_param4:
        _PRIORITY_OPTIONS = ["Standard (3-5 days)", "Express (1-2 days)", "Rush (Same day)"]
        selected_priority = st.selectbox(
            "Processing Priority", _PRIORITY_OPTIONS,
            key=f"priority_{_line_key(line)}"
        )
    
    # Fitting height if fitting required
    if _lp_fit_req or _lp_fit_ht:
        _FH_OPTIONS = ["", "12", "14", "16", "18", "20", "22", "24"]
        _fh_idx = _FH_OPTIONS.index(_lp_fit_ht) if _lp_fit_ht in _FH_OPTIONS else 0
        _proc_fh = st.selectbox(
            "↕ Fitting Height (mm)",
            _FH_OPTIONS, index=_fh_idx,
            key=f"fitting_ht_{_line_key(line)}"
        )
    else:
        _proc_fh = ""

    # Special instructions — pre-fill from lens_params
    special_instructions = st.text_area(
        "Special Instructions",
        value=_lp_inst,
        placeholder="Any special handling instructions...",
        key=f"instructions_{_line_key(line)}",
        height=80
    )

    
    st.markdown("---")
    
    # ==================================================
    # STEP 11: FINAL CALCULATIONS WITH BASE
    # ==================================================
    surf_final = calculate_surfacing_powers(
        sph, cyl, axis, eye_side,
        _cat_col or lens_category_raw or category,   # KT/Kryptok detection
        base_curve
    )
    
    st.markdown("#### 🔧 Final Tool Calculations")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Base Curve", f"{base_curve:.2f}D")
    c2.metric("Tool A", surf_final.get("tool_a") or "N/A")
    c3.metric("Tool B", surf_final.get("tool_b") or "N/A")
    
    # ==================================================
    # COMPLETE JOB CARD SUMMARY (clean UI — no raw JSON)
    # ==================================================
    with st.expander("📋 View Complete Job Card Details", expanded=False):

        # ── Row 1: Prescription + Surfacing Powers side by side ──────
        p_col, s_col = st.columns(2)

        with p_col:
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;"
                "padding:12px 16px;margin-bottom:8px;'>"
                "<div style='color:#60a5fa;font-weight:700;font-size:0.8rem;"
                "letter-spacing:.08em;margin-bottom:8px;'>📐 ORIGINAL PRESCRIPTION</div>",
                unsafe_allow_html=True,
            )
            rx_rows = [
                ("SPH", f"{sph:+.2f}"),
                ("CYL", f"{cyl:+.2f}"),
                ("AXIS", f"{axis}°"),
            ]
            if add_power is not None:
                rx_rows.append(("ADD", f"{float(add_power):+.2f}"))
            rx_rows.append(("Eye", eye_side or "—"))

            for label, val in rx_rows:
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"padding:3px 0;border-bottom:1px solid #1e293b;'>"
                    f"<span style='color:#94a3b8;font-size:0.85rem;'>{label}</span>"
                    f"<span style='color:#f1f5f9;font-weight:600;font-size:0.9rem;'>{val}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

        with s_col:
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;"
                "padding:12px 16px;margin-bottom:8px;'>"
                "<div style='color:#a78bfa;font-weight:700;font-size:0.8rem;"
                "letter-spacing:.08em;margin-bottom:8px;'>⚙️ SURFACING POWERS (Minus Cyl)</div>",
                unsafe_allow_html=True,
            )
            surf_rows = [
                ("SPH surf", f"{surf_final.get('sph_surf', 0):+.2f}"),
                ("CYL surf", f"{surf_final.get('cyl_surf', 0):+.2f}"),
                ("AXIS surf", f"{int(surf_final.get('axis_surf') or 0)}°"),
                ("Kryptok", "✅ Applied" if surf_final.get("kryptok_correction_applied") else "—"),
            ]
            for label, val in surf_rows:
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"padding:3px 0;border-bottom:1px solid #1e293b;'>"
                    f"<span style='color:#94a3b8;font-size:0.85rem;'>{label}</span>"
                    f"<span style='color:#f1f5f9;font-weight:600;font-size:0.9rem;'>{val}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

        # ── Row 2: Blank Info + Tool Settings side by side ───────────
        b_col, t_col = st.columns(2)

        with b_col:
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;"
                "padding:12px 16px;margin-bottom:8px;'>"
                "<div style='color:#34d399;font-weight:700;font-size:0.8rem;"
                "letter-spacing:.08em;margin-bottom:8px;'>🗃️ BLANK DETAILS</div>",
                unsafe_allow_html=True,
            )
            blank_rows = [
                ("Brand",     blank.get("brand", "—")),
                ("Material",  blank.get("material", "—")),
                ("Colour",    blank.get("colour") or "—"),
                ("Batch",     blank.get("batch_no") or "—"),
                ("Base",      f"{base_curve:.2f}D"),
            ]
            for label, val in blank_rows:
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"padding:3px 0;border-bottom:1px solid #1e293b;'>"
                    f"<span style='color:#94a3b8;font-size:0.85rem;'>{label}</span>"
                    f"<span style='color:#f1f5f9;font-weight:600;font-size:0.9rem;'>{val}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

        with t_col:
            st.markdown(
                "<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:8px;"
                "padding:12px 16px;margin-bottom:8px;'>"
                "<div style='color:#f59e0b;font-weight:700;font-size:0.8rem;"
                "letter-spacing:.08em;margin-bottom:8px;'>🔧 TOOL SETTINGS</div>",
                unsafe_allow_html=True,
            )
            tool_rows = [
                ("Base Curve",    f"{base_curve:.2f}D"),
                ("Tool A",        str(surf_final.get("tool_a") or "—")),
                ("Tool B",        str(surf_final.get("tool_b") or "—")),
                ("Diameter",      final_diameter),
                ("Frame",         selected_frame_type),
                ("Edge Finish",   selected_edge_type),
                ("Priority",      selected_priority),
            ]
            for label, val in tool_rows:
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"padding:3px 0;border-bottom:1px solid #1e293b;'>"
                    f"<span style='color:#94a3b8;font-size:0.85rem;'>{label}</span>"
                    f"<span style='color:#f1f5f9;font-weight:600;font-size:0.9rem;'>{val}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

        # ── Special instructions (if any) ─────────────────────────────
        if special_instructions:
            st.markdown(
                f"<div style='background:#1c1917;border:1px solid #44403c;border-radius:8px;"
                f"padding:10px 14px;'>"
                f"<span style='color:#a8a29e;font-size:0.8rem;'>📝 SPECIAL INSTRUCTIONS</span><br>"
                f"<span style='color:#e7e5e4;font-size:0.9rem;'>{special_instructions}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
    
    # ==================================================
    # SAVE JOB CARD & PRINT OPTIONS
    # ==================================================
    if not show_buttons:
        # Caller (production panel) owns the save/print buttons — skip them here
        return

    st.markdown("---")
    
    _save_col, _print_col, _label_col, _cr80_col = st.columns(4)
    
    with _save_col:
        if st.button(
            "💾 Save & Print",
            type="primary",
            key=f"save_print_{_line_key(line)}"
        ):
            # Use this eye's resolved base, not a shared R/L value.
            _pair_base_final = st.session_state.get(_base_selection_key(line))
            _eff_inventory_base = (
                float(_pair_base_final)
                if _pair_base_final is not None
                else inventory_base
            )

            line["surfacing_data"] = {
                "blank_id": str(blank["id"]),
                "blank_brand": blank["brand"],
                "blank_material": blank["material"],
                "blank_colour": blank["colour"],
                "blank_batch": blank["batch_no"],
                "blank_cost_per_pcs": float(blank.get("cost_price") or 0),
                "blank_add": float(blank.get("add_power") or 0),
                "add_power_selected": selected_add,
                "base_curve": base_curve,
                "base_curve_recommended": _eff_inventory_base or system_recommended_base,
                "inventory_base": _eff_inventory_base,
                "selected_base": base_curve,
                "recommended_base": _eff_inventory_base,
                "system_recommended_base": system_recommended_base,
                "sph_surf": surf_final["sph_surf"],
                "cyl_surf": surf_final["cyl_surf"],
                "axis_surf": surf_final["axis_surf"],
                "tool_a": surf_final.get("tool_a"),
                "tool_b": surf_final.get("tool_b"),
                "kryptok_applied": surf_final.get("kryptok_correction_applied", False),
                "diameter": final_diameter,
                "frame_type": selected_frame_type,
                "edge_finish": selected_edge_type,
                "priority": selected_priority,
                "special_instructions": special_instructions,
            }
            
            _save_guard_key = f"jc_saving_{_line_key(line)}"
            if st.session_state.get(_save_guard_key):
                st.stop()
            st.session_state[_save_guard_key] = True

            _already_saved = False
            try:
                from modules.sql_adapter import run_query as _rq
                _lid_check = (line.get("line_id") or line.get("id") or "").strip()
                if _lid_check:
                    _lp_rows = _rq(
                        "SELECT lens_params FROM order_lines WHERE id = %(l)s::uuid LIMIT 1",
                        {"l": _lid_check}
                    )
                    if _lp_rows:
                        import json as _jchk
                        _lp = _lp_rows[0].get("lens_params") or {}
                        if isinstance(_lp, str):
                            try: _lp = _jchk.loads(_lp)
                            except Exception: _lp = {}
                        _already_saved = bool(_lp.get("surfacing_data"))
            except Exception:
                pass

            if not _already_saved:
                _save_eye_qty, _save_eye_label = _blank_eye_qty(blank, eye_side, is_eye_specific)
                if _save_eye_qty <= 0:
                    _blank_name = " · ".join(
                        str(x or "")
                        for x in (blank.get("brand"), blank.get("material"), blank.get("batch_no"))
                        if str(x or "").strip()
                    )
                    if is_eye_specific:
                        st.error(
                            f"❌ {_save_eye_label} eye quantity is zero for {_blank_name}. "
                            "Cannot assign this blank. Select another supplier/base with stock for this eye."
                        )
                    else:
                        st.error(
                            f"❌ Blank quantity is zero for {_blank_name}. "
                            "Cannot assign this blank. Select another blank with stock."
                        )
                    st.session_state.pop(f"jc_saving_{_line_key(line)}", None)
                    return

            if _already_saved:
                line["surfacing_data"] = surfacing_data
                _persist_surfacing_to_db(line)
                _upsert_job_master(line, order)
                st.success("✅ Job card updated!")
            else:
                success = update_blank_quantity(
                    blank_id=str(blank["id"]),
                    qty_change=-1,
                    eye_side=eye_side if is_eye_specific else None
                )
                if success:
                    _persist_surfacing_to_db(line)
                    _upsert_job_master(line, order)
                    st.success("✅ Saved & inventory updated!")
                else:
                    st.error("❌ Failed to update inventory")
                    st.session_state.pop(f"jc_saving_{_line_key(line)}", None)
                    return

            st.session_state[f"show_print_{_line_key(line)}"] = True
            st.rerun()

    with _print_col:
        if st.button("🖨️ Job Card", key=f"print_jc_{_line_key(line)}", 
                    use_container_width=True):
            if line.get("surfacing_data"):
                st.session_state[f"show_print_{_line_key(line)}"] = True
            else:
                st.warning("Save job card first")
            st.rerun()

    with _label_col:
        if st.button("🏷️ Barcode", key=f"print_label_{_line_key(line)}", 
                    use_container_width=True):
            st.session_state[f"show_label_{_line_key(line)}"] = True
            st.rerun()

    with _cr80_col:
        if st.button("💳 CR80", key=f"print_cr80_{_line_key(line)}", 
                    use_container_width=True):
            st.session_state[f"show_cr80_{_line_key(line)}"] = True
            st.rerun()

    if st.session_state.get(f"show_label_{_line_key(line)}"):
        _render_label_for_job_card(line, order)
        if st.button("✕ Close Label", key=f"close_label_{_line_key(line)}"):
            st.session_state.pop(f"show_label_{_line_key(line)}", None)
            st.rerun()

    if st.session_state.get(f"show_cr80_{_line_key(line)}"):
        _render_cr80_for_job_card(line, order)
        if st.button("✕ Close CR80", key=f"close_cr80_{_line_key(line)}"):
            st.session_state.pop(f"show_cr80_{_line_key(line)}", None)
            st.rerun()

    # ================================
    # PRINT PREVIEW RENDERING
    # ================================
    if st.session_state.get(f"show_print_{_line_key(line)}"):
        st.markdown("---")
        st.markdown("## 🖨️ Print Preview")
        render_job_card_print(line, order)
        if st.button("✕ Close Print", key=f"close_print_{_line_key(line)}"):
            st.session_state.pop(f"show_print_{_line_key(line)}", None)
            st.rerun()


# ======================================================
# PRINT VIEW
# ======================================================

def render_job_card_print(line: Dict, order: Dict):
    """
    Job card print — opens in new tab.
    Always fetches BOTH eyes from DB using order_id so R+L always print together.
    """
    surf = line.get("surfacing_data") or {}
    if not surf:
        # Try loading from lens_params in DB before giving up
        try:
            import json as _jlp2
            from modules.sql_adapter import run_query as _rq_lp
            _lid2 = (line.get("line_id") or line.get("id") or "").strip()
            if _lid2:
                _rows2 = _rq_lp(
                    "SELECT lens_params FROM order_lines WHERE id = %(l)s::uuid LIMIT 1",
                    {"l": _lid2}
                )
                if _rows2:
                    _lp2 = _rows2[0].get("lens_params") or {}
                    if isinstance(_lp2, str):
                        try: _lp2 = _jlp2.loads(_lp2)
                        except Exception: _lp2 = {}
                    surf = _lp2.get("surfacing_data") or {}
                    if surf:
                        line = dict(line)
                        line["surfacing_data"] = surf
        except Exception:
            pass

    if not surf:
        st.warning("⚠️ No surfacing data — save the job card first.")
        return

    # Always try to fetch both eyes from DB so R+L both appear
    r_line = None
    l_line = None
    _oid = str(order.get("id") or "").strip()
    if _oid:
        try:
            import json as _jrl
            from modules.sql_adapter import run_query as _rq_rl
            _all_lines = _rq_rl(
                "SELECT ol.*, jm.current_stage AS lab_stage, "
                "       p.product_name, p.index_value, p.coating, p.coating_type, p.material "
                "FROM order_lines ol "
                "LEFT JOIN job_master jm ON jm.order_line_id = ol.id "
                "LEFT JOIN products p ON p.id = ol.product_id "
                "WHERE ol.order_id = %(oid)s::uuid "
                "  AND COALESCE(ol.is_deleted, FALSE) = FALSE "
                "ORDER BY ol.eye_side",
                {"oid": _oid}
            ) or []
            for _ol in _all_lines:
                # Merge lens_params.surfacing_data into each line dict
                _lp = _ol.get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _jrl.loads(_lp)
                    except Exception: _lp = {}
                if isinstance(_lp, dict) and not _ol.get("surfacing_data"):
                    _ol = dict(_ol)
                    _ol["surfacing_data"] = _lp.get("surfacing_data") or {}
                _eye = str(_ol.get("eye_side") or "").upper()[:1]
                if _eye == "R":
                    r_line = _ol
                elif _eye == "L":
                    l_line = _ol
        except Exception:
            pass

    # Fallback: use the line we were given if DB fetch found nothing
    if r_line is None and l_line is None:
        eye = str(line.get("eye_side", "")).upper()[:1]
        r_line = line if eye == "R" else None
        l_line = line if eye == "L" else None

    _open_jc_print_window(r_line, l_line, order)


def _open_jc_print_window(r_line, l_line, order: Dict):
    """Build job card HTML and open in new tab.
    Guard: compact view passes placeholder rows (product_name='N job(s)').
    When detected, reload real order lines from DB before printing.
    """
    # Detect and fix placeholder rows from compact view
    _check = r_line or l_line or {}
    if "job(s)" in str(_check.get("product_name") or ""):
        _oid_jc = str(order.get("id") or "").strip()
        if _oid_jc:
            try:
                from modules.sql_adapter import run_query as _rq_jc
                _real = _rq_jc(
                    "SELECT ol.*, jm.current_stage AS lab_stage, "
                    "       p.product_name, p.index_value, p.coating, p.coating_type, p.material "
                    "FROM order_lines ol "
                    "LEFT JOIN job_master jm ON jm.order_line_id = ol.id "
                    "LEFT JOIN products p ON p.id = ol.product_id "
                    "WHERE ol.order_id = %(oid)s::uuid "
                    "  AND COALESCE(ol.is_deleted, FALSE) = FALSE "
                    "ORDER BY ol.eye_side", {"oid": _oid_jc}
                ) or []
                r_line = next((l for l in _real if str(l.get("eye_side","")).upper()[:1]=="R"), r_line)
                l_line = next((l for l in _real if str(l.get("eye_side","")).upper()[:1]=="L"), l_line)
            except Exception:
                pass

    import math as _m
    import base64 as _b64
    import datetime as _dt
    import html as _html
    import streamlit.components.v1 as _comp

    def _pf(v, d=0.0):
        try: f=float(v or d); return d if _m.isnan(f) or _m.isinf(f) else f
        except Exception as e:
            log.debug("Job-card dict fallback: %s", e)
        return d
    def _pi(v): return int(_pf(v))
    def _sgn(v):
        f=_pf(v)
        return f"+{f:.2f}" if f>=0 else f"{f:.2f}"
    def _cell(v, blank=False):
        if blank: return ""
        return _sgn(v)

    def _safe_text(v, default="-"):
        """Return an HTML-escaped string, normalising common Unicode punctuation."""
        raw = v if v not in (None, "", "None") else default
        txt = str(raw)
        txt = (txt.replace("\u2014", "-")   # em dash
                  .replace("\u2013", "-")   # en dash
                  .replace("\u2018", "'")   # left single quote
                  .replace("\u2019", "'")   # right single quote
                  .replace("\u00b1", "+/-") # ± sign
                  .replace("±",      "+/-") # ± as literal bytes (fallback)
               )
        # Fix latin-1 mojibake: â = 0xC2 read as latin-1 instead of UTF-8 ±
        txt = txt.replace("\u00c2\u00b1", "+/-")   # â± (latin-1 ± mojibake)
        txt = txt.replace("\u00c2\u00b0", "deg")   # â° (latin-1 ° mojibake)
        txt = txt.replace("\u00e2\u0080\u0094", "-")  # â€" em-dash mojibake
        txt = txt.replace("\u00e2\u0080\u0093", "-")  # â€" en-dash mojibake
        return _html.escape(txt)

    def _clean_tool(v):
        """Format tool_a / tool_b for print.
        power_engine stores these as floats or strings like '+3.50', '±1.25', '-2.00'.
        Returns a plain signed decimal string, or '' if empty/None."""
        if v is None or str(v).strip() in ("", "None", "—", "-", "N/A"):
            return ""
        s = str(v).strip()
        # Strip ± variants — tool values are just numbers
        s = (s.replace("\u00b1", "").replace("±", "")
              .replace("\u00c2\u00b1", "").replace("â", ""))
        s = s.strip()
        if not s:
            return ""
        # If it's a plain float, format with sign
        try:
            f = float(s)
            if abs(f - round(f)) < 0.0001:
                return str(int(round(f)))
            return f"{f:.2f}".rstrip("0").rstrip(".")
        except ValueError:
            pass
        # If it contains a slash (e.g. "+3.50 / -1.25"), return as-is after escaping
        return _html.escape(s.replace("\u2014", "-").replace("\u2013", "-").replace("+", ""))

    def _raw_text(v):
        if v in (None, "", "None", "—", "-"):
            return ""
        return str(v).strip()

    def _product_display(ln):
        ln = ln or {}
        lp = ln.get("lens_params") or {}
        if isinstance(lp, str):
            try:
                import json as _jprod
                lp = _jprod.loads(lp)
            except Exception:
                lp = {}
        if not isinstance(lp, dict):
            lp = {}

        name = _raw_text(ln.get("product_name") or lp.get("product_name"))
        idx = _raw_text(
            ln.get("index_value")
            or ln.get("lens_index")
            or lp.get("index_value")
            or lp.get("lens_index")
            or lp.get("index")
            or lp.get("Lens Index")
        )
        brand = _raw_text(
            ln.get("brand")
            or ln.get("brand_name")
            or ln.get("selected_brand")
            or lp.get("brand")
            or lp.get("brand_name")
            or lp.get("selected_brand")
        )
        coating = _raw_text(
            ln.get("coating")
            or ln.get("coating_type")
            or lp.get("coating")
            or lp.get("coating_type")
            or lp.get("lens_coating")
        )

        parts = [name or "-"]
        upper_name = name.upper()
        if brand and brand.upper() not in upper_name:
            parts.append(f"Brand {brand}")
        if idx and idx.upper() not in upper_name:
            parts.append(f"Index {idx}")
        if coating and coating.upper() not in upper_name:
            parts.append(f"Coating {coating}")
        return _safe_text(" | ".join(parts), "-")

    _raw_order_no = str(order.get("order_no") or "")
    _route_hint = " ".join(
        str(x or "") for x in [
            _raw_order_no,
            order.get("production_ref"),
            order.get("service_production_type"),
            order.get("order_type"),
            (r_line or {}).get("production_ref") if r_line else "",
            (l_line or {}).get("production_ref") if l_line else "",
        ]
    ).upper()
    if _raw_order_no.upper().endswith("-F") or "FITTING" in _route_hint:
        _service_label = "FITTING"
    elif _raw_order_no.upper().endswith("-C") or "COLOUR" in _route_hint or "COLOR" in _route_hint:
        _service_label = "COLOURING"
    else:
        _service_label = ""
    _tool_hdr_a = _service_label or "TOOL A"
    _tool_hdr_b = _service_label or "TOOL B"

    order_no  = _safe_text(_raw_order_no, "-")
    patient   = _safe_text(order.get("patient_name"), "-")
    today     = _dt.date.today().strftime("%d-%m-%Y")

    # Shop info
    shop_name = "DV Optical"
    shop_phone = ""
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name  = _safe_text(_sh.get("shop_name", shop_name), shop_name)
        shop_phone = _sh.get("shop_phone","")
    except Exception: pass

    def _eye_data(ln):
        if not ln: return None
        import json as _jlp
        surf = ln.get("surfacing_data") or {}
        lp   = ln.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = _jlp.loads(lp)
            except Exception as e:
                log.debug("Could not parse lens_params: %s", e)
                lp = {}
        # Prefer surfacing_data on the line dict; fall back to lens_params.surfacing_data
        surf = (surf if isinstance(surf, dict) and surf
                else (lp.get("surfacing_data") or {}))
        _sph = _pf(ln.get("sph"))
        _cyl = _pf(ln.get("cyl"))
        _axis = _pi(ln.get("axis"))
        _eye = str(ln.get("eye_side") or "").upper().strip()
        _base = _pf(
            surf.get("base_curve")
            or surf.get("selected_base")
            or surf.get("base")
            or surf.get("base_selected")
            or surf.get("recommended_base")
        )
        _cat = (
            ln.get("type")
            or ln.get("category")
            or ln.get("lens_category")
            or lp.get("lens_category")
            or lp.get("category")
            or ""
        )
        if not _base:
            try:
                _pre_calc = calculate_surfacing_powers(_sph, _cyl, _axis, _eye, _cat, None) or {}
                _base = recommend_base_curve(_pre_calc.get("sph_surf", _sph), _pre_calc.get("cyl_surf", _cyl))
            except Exception as e:
                log.debug("Print-time base fallback failed: %s", e)
        _calc = {}
        if not all(k in surf and surf.get(k) not in (None, "") for k in ("sph_surf", "cyl_surf", "axis_surf", "tool_a", "tool_b")):
            try:
                _calc = calculate_surfacing_powers(_sph, _cyl, _axis, _eye, _cat, _base or None) or {}
            except Exception as e:
                log.debug("Print-time surfacing fallback failed: %s", e)
                _calc = {}

        def _sv(*keys, default=None):
            for key in keys:
                val = surf.get(key)
                if val not in (None, "", "None", "—", "-"):
                    return val
            return default

        return {
            "sph":    _sph,
            "cyl":    _cyl,
            "axis":   _axis,
            "add":    _pf(ln.get("add_power")),
            "sph_s":  _pf(_sv("sph_surf", "surf_sph", "sph_s", default=_calc.get("sph_surf"))),
            "cyl_s":  _pf(_sv("cyl_surf", "surf_cyl", "cyl_s", default=_calc.get("cyl_surf"))),
            "axis_s": _pi(_sv("axis_surf", "surf_axis", "axis_s", "axis_final", default=_calc.get("axis_surf"))),
            "base":   _base,
            "tool_a": _clean_tool(_sv("tool_a", "dia_tool_a", "toolA", default=_calc.get("tool_a"))),
            "tool_b": _clean_tool(_sv("tool_b", "dia_tool_b", "toolB", default=_calc.get("tool_b"))),
            "blank":  _safe_text(f"{surf.get('blank_brand','')} {surf.get('blank_material','')}".strip(), ""),
            "colour": _safe_text(surf.get("blank_colour") or "Clear", "Clear"),
            "dia":    _safe_text(surf.get("diameter") or "", ""),
            "frame":  _safe_text(surf.get("frame_type") or lp.get("frame_type") or "", ""),
            "notes":  _safe_text(surf.get("special_instructions") or "", ""),
            "batch":  _safe_text(surf.get("blank_batch") or "", ""),
            "service": str(
                lp.get("service_production_type")
                or lp.get("charge_type")
                or lp.get("service_group")
                or ln.get("production_ref")
                or ""
            ).upper(),
        }

    R = _eye_data(r_line)
    L = _eye_data(l_line)
    _line_service_hint = " ".join(str((d or {}).get("service") or "") for d in (R, L)).upper()
    if not _service_label:
        if "FITTING" in _line_service_hint or _line_service_hint.endswith("-F"):
            _service_label = "FITTING"
        elif "COLOUR" in _line_service_hint or "COLOR" in _line_service_hint or _line_service_hint.endswith("-C"):
            _service_label = "COLOURING"
    _tool_hdr_a = _service_label or "Tool A"
    _tool_hdr_b = _service_label or "Tool B"

    product = _product_display(r_line or l_line)

    # Phone number box (3403-style from image)
    phone_box = shop_phone or "-"

    def _resolve_customer_barcode() -> str:
        """Resolve stable billing lookup code: patient record_no/PAT or party barcode."""
        oid = str(order.get("id") or "").strip()
        pid = str(order.get("party_id") or order.get("patient_id") or order.get("customer_id") or "").strip()
        pname = str(order.get("party_name") or order.get("patient_name") or "").strip()
        pmob = str(order.get("party_mobile") or order.get("patient_mobile") or order.get("mobile") or "").strip()

        def _compact_party_barcode(party_id: str, existing: str, gstin: str) -> str:
            existing = str(existing or "").strip().upper()
            gstin = str(gstin or "").strip().upper()
            existing_clean = "".join(ch for ch in existing if ch.isalnum())
            # Keep an existing short scanner code. Replace GSTIN/very-long values with a stable party code.
            if existing_clean and len(existing_clean) <= 12 and existing_clean != gstin:
                return existing_clean
            pid_clean = "".join(ch for ch in str(party_id or "") if ch.isalnum()).upper()
            short_code = f"P{pid_clean[-10:]}" if pid_clean else (existing_clean[-10:] or gstin[-10:] or "")
            if short_code:
                try:
                    from modules.sql_adapter import run_write
                    run_write(
                        """
                        UPDATE parties
                        SET barcode=%s
                        WHERE id=%s::uuid
                          AND (
                            barcode IS NULL OR barcode='' OR barcode=%s
                            OR length(regexp_replace(COALESCE(barcode,''),'[^A-Za-z0-9]','','g')) > 12
                          )
                        """,
                        (short_code, party_id, gstin),
                    )
                except Exception as e:
                    log.debug("Could not persist compact party barcode: %s", e)
            return short_code or existing_clean or gstin

        try:
            from modules.sql_adapter import run_query
            if oid and not (pid or pmob):
                o_rows = run_query(
                    """
                    SELECT COALESCE(party_id::text,'') AS party_id,
                           COALESCE(party_name,'') AS party_name,
                           COALESCE(patient_name,'') AS patient_name,
                           COALESCE(patient_mobile,'') AS patient_mobile
                    FROM orders
                    WHERE id=%s::uuid
                    LIMIT 1
                    """,
                    (oid,),
                ) or []
                if o_rows:
                    pid = str(o_rows[0].get("party_id") or pid or "").strip()
                    pname = str(o_rows[0].get("party_name") or o_rows[0].get("patient_name") or pname or "").strip()
                    pmob = str(o_rows[0].get("patient_mobile") or pmob or "").strip()

            if pid:
                p_rows = run_query(
                    """
                    SELECT COALESCE(record_no,'') AS record_no,
                           COALESCE(barcode,'') AS barcode
                    FROM patients
                    WHERE id=%s::uuid
                    LIMIT 1
                    """,
                    (pid,),
                ) or []
                if p_rows:
                    rec = str(p_rows[0].get("record_no") or "").strip()
                    pat = str(p_rows[0].get("barcode") or "").strip()
                    return rec or pat or pid.replace("-", "")[:12].upper()

                party_rows = run_query(
                    """
                    SELECT COALESCE(barcode,'') AS barcode,
                           COALESCE(gstin,'') AS gstin,
                           COALESCE(mobile,'') AS mobile,
                           COALESCE(party_name,'') AS party_name
                    FROM parties
                    WHERE id=%s::uuid
                    LIMIT 1
                    """,
                    (pid,),
                ) or []
                if party_rows:
                    return _compact_party_barcode(
                        pid,
                        str(party_rows[0].get("barcode") or "").strip(),
                        str(party_rows[0].get("gstin") or "").strip(),
                    ) or str(party_rows[0].get("mobile") or "").strip() or str(party_rows[0].get("party_name") or "").strip()

            if pmob:
                p_rows = run_query(
                    """
                    SELECT COALESCE(record_no,'') AS record_no,
                           COALESCE(barcode,'') AS barcode
                    FROM patients
                    WHERE regexp_replace(COALESCE(mobile,''),'\\D','','g') = %s
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    ("".join(ch for ch in pmob if ch.isdigit())[-10:],),
                ) or []
                if p_rows:
                    return str(p_rows[0].get("record_no") or p_rows[0].get("barcode") or "").strip()
        except Exception as e:
            log.debug("Customer barcode resolver fallback: %s", e)

        return pname or pmob or order_no

    # Barcode values: one order barcode + one party/retail-customer barcode.
    def _short_code(prefix: str, raw: str, max_len: int = 16) -> str:
        clean = "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()
        if len(clean) > max_len:
            clean = clean[-max_len:]
        if clean.startswith(("P", "C", "PAT")):
            return clean
        return f"{prefix}{clean}" if clean else prefix

    _order_clean = "".join(ch for ch in order_no if ch.isalnum())
    _customer_source = _resolve_customer_barcode()
    bc_order = _short_code("O", _order_clean, 14)
    bc_customer = _short_code("C", _customer_source or _order_clean, 16)

    def _barcode_img(val):
        """Generate a real scannable Code128 barcode as inline SVG.
        Uses python-barcode library for laser-printable, scanner-readable output.
        Falls back to a text-only box if library unavailable.
        """
        val = str(val or "").strip()
        if not val:
            return ""
        try:
            import barcode as _bc_lib
            from barcode.writer import SVGWriter as _SVGWriter
            import io as _io_bc
            _bc = _bc_lib.get("code128", val, writer=_SVGWriter())
            _buf = _io_bc.BytesIO()
            _bc.write(_buf, options={
                "write_text":    True,
                "module_height": 10.0,   # bar height in mm
                "module_width":  0.25,   # narrow bar width in mm — wider = more readable on laser
                "quiet_zone":    2.0,
                "font_size":     7,
                "text_distance": 1.5,
            })
            _svg_raw = _buf.getvalue().decode("utf-8")
            # Strip XML declaration — keep only <svg>...</svg>
            _svg = _svg_raw[_svg_raw.find("<svg"):]
            # Remove fixed width/height so it scales with CSS
            import re as _re_bc
            _svg = _re_bc.sub(r'width="[^"]*"',  'width="100%"',  _svg, count=1)
            _svg = _re_bc.sub(r'height="[^"]*"', 'height="auto"', _svg, count=1)
            return f'<div style="max-width:160px;min-width:100px">{_svg}</div>'
        except Exception as _be:
            # Fallback: human-readable text box — at least shows the value
            return (
                f"<div style='border:1px solid #000;display:inline-block;"
                f"padding:3px 6px;font-family:monospace;font-size:7pt;"
                f"letter-spacing:1px'>{val}<br>"
                f"<span style='font-size:5pt;color:#666'>"
                f"[barcode lib unavailable: {_be}]</span></div>"
            )

    def _td(val, bold=False, empty=False):
        style = "border:1px solid #000;padding:2px 5px;text-align:center;"
        if bold: style += "font-weight:900;"
        if empty: style += "background:#f9f9f9;"
        return f"<td style='{style}'>{'' if empty else val}</td>"

    def _row_rx(R_data, L_data):
        """RX row"""
        return f"""<tr>
            {_td(_sgn(R_data['sph']) if R_data else '', empty=not R_data)}
            {_td(_sgn(R_data['cyl']) if R_data else '', empty=not R_data)}
            {_td(R_data['axis'] if R_data else '', empty=not R_data)}
            {_td(_sgn(R_data['add']) if R_data else '', empty=not R_data)}
            {_td(_sgn(L_data['sph']) if L_data else '', empty=not L_data)}
            {_td(_sgn(L_data['cyl']) if L_data else '', empty=not L_data)}
            {_td(L_data['axis'] if L_data else '', empty=not L_data)}
            {_td(_sgn(L_data['add']) if L_data else '', empty=not L_data)}
        </tr>"""

    def _row_surf(R_data, L_data):
        """Surfacing powers row"""
        return f"""<tr>
            {_td(_sgn(R_data['sph_s']) if R_data else '', empty=not R_data)}
            {_td(_sgn(R_data['cyl_s']) if R_data else '', empty=not R_data)}
            {_td(R_data['axis_s'] if R_data else '', empty=not R_data)}
            {_td('', empty=True)}
            {_td(_sgn(L_data['sph_s']) if L_data else '', empty=not L_data)}
            {_td(_sgn(L_data['cyl_s']) if L_data else '', empty=not L_data)}
            {_td(L_data['axis_s'] if L_data else '', empty=not L_data)}
            {_td('', empty=True)}
        </tr>"""

    def _row_tools(R_data, L_data):
        return f"""<tr>
            {_td(f"{R_data['base']:.2f}" if R_data and R_data['base'] else '', empty=not R_data or not R_data['base'])}
            {_td(R_data['tool_a'] if R_data else '', empty=not R_data)}
            {_td(R_data['tool_b'] if R_data else '', empty=not R_data)}
            {_td('', empty=True)}
            {_td(f"{L_data['base']:.2f}" if L_data and L_data['base'] else '', empty=not L_data or not L_data['base'])}
            {_td(L_data['tool_a'] if L_data else '', empty=not L_data)}
            {_td(L_data['tool_b'] if L_data else '', empty=not L_data)}
            {_td('', empty=True)}
        </tr>"""

    frame_val  = (R or L or {}).get("frame", "")
    dia_val    = (R or L or {}).get("dia", "")
    blank_val  = (R or L or {}).get("blank", "")
    notes_val  = (R or L or {}).get("notes", "")
    # Per-eye blank + colour for tools section
    r_blank    = (R or {}).get("blank", "")
    r_colour   = (R or {}).get("colour", "")
    l_blank    = (L or {}).get("blank", "")
    l_colour   = (L or {}).get("colour", "")
    # Material/brand split (blank = "Brand Material", e.g. "Essilor 1.60")
    _bsplit    = blank_val.split() if blank_val else []
    blank_brand_val    = _bsplit[0] if _bsplit else ""
    blank_material_val = " ".join(_bsplit[1:]) if len(_bsplit) > 1 else blank_val
    _prod_line = r_line or l_line or {}
    _prod_lp = _prod_line.get("lens_params") or {}
    if isinstance(_prod_lp, str):
        try:
            import json as _jprod_lp
            _prod_lp = _jprod_lp.loads(_prod_lp)
        except Exception:
            _prod_lp = {}
    if not isinstance(_prod_lp, dict):
        _prod_lp = {}
    product_brand_val = _safe_text(
        _raw_text(_prod_line.get("brand") or _prod_line.get("brand_name") or _prod_lp.get("brand") or _prod_lp.get("brand_name") or _prod_lp.get("selected_brand")),
        "",
    )
    product_index_val = _safe_text(
        _raw_text(_prod_line.get("index_value") or _prod_lp.get("index_value") or _prod_lp.get("lens_index") or _prod_lp.get("index") or _prod_lp.get("Lens Index")),
        "",
    )
    product_coating_val = _safe_text(
        _raw_text(_prod_line.get("coating") or _prod_line.get("coating_type") or _prod_lp.get("coating") or _prod_lp.get("coating_type") or _prod_lp.get("lens_coating")),
        "",
    )
    # Party name (dealer/shop who placed the order)
    party_val  = _safe_text(order.get("party_name") or "", "")

    def _power_rows():
        rows = []
        for eye_lbl, data in (("R", R), ("L", L)):
            if not data:
                continue
            if _service_label:
                tool_cells = f'<td class="service-cell" colspan="2">{_service_label}</td>'
            else:
                tool_cells = (
                    f'<td class="tool-big">{data["tool_a"]}</td>'
                    f'<td class="tool-big">{data["tool_b"]}</td>'
                )
            rows.append(
                f"""<tr>
                  <td rowspan="2" class="eye-name">{eye_lbl}</td>
                  <td class="lbl">RX</td>
                  <td class="power-val">{_sgn(data['sph'])}</td>
                  <td class="power-val">{_sgn(data['cyl'])}</td>
                  <td class="power-val">{data['axis']}</td>
                  <td class="power-val">{_sgn(data['add'])}</td>
                  <td style="background:#f9f9f9"></td>
                  <td style="background:#f9f9f9"></td>
                  <td style="background:#f9f9f9"></td>
                </tr>
                <tr>
                  <td class="lbl">Surfacing</td>
                  <td class="power-val">{_sgn(data['sph_s'])}</td>
                  <td class="power-val">{_sgn(data['cyl_s'])}</td>
                  <td class="print-hi">{data['axis_s']}</td>
                  <td style="background:#f9f9f9"></td>
                  <td class="power-val">{f"{data['base']:.2f}" if data['base'] else ""}</td>
                  {tool_cells}
                </tr>"""
            )
        return "".join(rows)

    def _tool_rows():
        rows = []
        for eye_lbl, data in (("RIGHT", R), ("LEFT", L)):
            if not data:
                continue
            if _service_label:
                tool_cells = f'<td class="service-cell" colspan="2">{_service_label}</td>'
            else:
                tool_cells = (
                    f'<td class="tool-big">{data["tool_a"]}</td>'
                    f'<td class="tool-big">{data["tool_b"]}</td>'
                )
            rows.append(f"<tr><td class='eye-name'>{eye_lbl}</td>{tool_cells}</tr>")
        return "".join(rows)

    _print_html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
  @page {{ size: 210mm 148mm; margin: 4mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-weight: 700; }}
  html, body {{
    width: 210mm; min-height: 148mm; margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 7pt; background: #fff; color: #000;
  }}
  .page {{
    width: 202mm; height: 140mm;
    margin: 0 auto;
    display: flex; flex-direction: column; gap: 1mm;
    overflow: hidden;
  }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ border: 1px solid #000; padding: 1px 3px; text-align: center;
        background: #e0e0e0; font-size: 6.5pt; font-weight: 700; }}
  td {{ border: 1px solid #000; padding: 1.5px 4px; font-size: 8pt;
        font-weight: 700; text-align: center; }}
  td.lbl {{ text-align: left; background: #f0f0f0; font-size: 7.5pt; white-space: nowrap; }}
  .eye-name {{ background:#0f172a !important; color:#fff; font-size:13pt !important; font-weight:900 !important; letter-spacing:.08em; }}
  .power-val {{ font-size: 10pt !important; font-weight: 900 !important; }}
  .print-hi {{ background: #fff2a8 !important; font-size: 12pt !important; font-weight: 900 !important; }}
  .service-cell {{ background: #dbeafe !important; font-size: 12pt !important; font-weight: 900 !important; letter-spacing: .08em; }}
  .tool-big {{ background: #fff2a8 !important; font-size: 12pt !important; font-weight: 900 !important; }}
  td.info {{ text-align: left; border: none; padding: 0.5px 2px; font-size: 7pt; }}
  td.info span {{ font-weight: 700; }}
  .hdr {{ text-align: center; border-bottom: 1.5px solid #000; padding-bottom: 1mm; }}
  .shop {{ font-size: 11pt; font-weight: 900; text-decoration: underline; letter-spacing: 0.5px; }}
  .phone {{ font-size: 7pt; font-weight: 700; }}
  .eye-hdr {{ background: #0f172a; color: #fff; text-align: center;
              font-size: 7.5pt; font-weight: 900; padding: 1px 3px;
              letter-spacing: 1px; }}
  .eye-hdr.right {{ background: #1e3a5f; }}
  .eye-hdr.left  {{ background: #1a3a1a; }}
  .product-name {{ text-align:left !important; font-size:10pt !important; font-weight:900 !important; }}
  .barcodes {{ display: flex; gap: 2mm; }}
  .bc-box {{ border: 1px solid #000; padding: 1mm 2mm; flex: 1;
             text-align: center; font-size: 6pt; min-height: 22mm; overflow:hidden; }}
  .bc-eye {{ font-size: 6.2pt; font-weight: 900; margin-bottom: 0.2mm; }}
  .no-print {{ display: block; }}
  @media print {{
    html, body {{
      width: 210mm !important;
      height: 148mm !important;
      margin: 0 !important;
      overflow: hidden !important;
    }}
    .page {{
      width: 202mm !important;
      height: 140mm !important;
      margin: 0 !important;
      page-break-after: avoid;
      break-after: avoid;
      overflow: hidden !important;
    }}
    .no-print {{ display: none !important; }}
  }}
  .print-btn {{ margin: 6px 0; padding: 7px 22px; background: #1e40af; color: #fff;
                border: none; border-radius: 5px; font-size: 11px; font-weight: 700;
                cursor: pointer; }}
</style>
</head><body>
<div class="page">

<!-- HEADER -->
<div class="hdr">
  <div class="shop">{shop_name}</div>
  {f'<div class="phone">&#128222; {shop_phone}</div>' if shop_phone else ''}
</div>

<!-- ORDER INFO (compact single row table) -->
<table>
  <tr>
    <td class="info"><span>Order No:</span> {order_no}</td>
    <td class="info"><span>Date:</span> {today}</td>
    <td class="info"><span>Patient:</span> {patient}</td>
    <td class="info"><span>Party/Dealer:</span> {party_val}</td>
    <td class="info"><span>Ph:</span> {phone_box}</td>
  </tr>
  <tr>
    <td class="info"><span>Brand:</span> {product_brand_val or blank_brand_val}</td>
    <td class="info"><span>Index:</span> {product_index_val or blank_material_val}</td>
    <td class="info"><span>Coating:</span> {product_coating_val}</td>
    <td class="info"><span>Colour:</span> {(R or L or {{}}).get('colour','Clear')}</td>
    <td class="info"><span>Frame:</span> {frame_val} / Dia {dia_val}</td>
  </tr>
</table>

<!-- RX + SURFACING POWERS -->
<table>
  <tr>
    <th style="width:11mm">Eye</th>
    <th style="width:20mm">Type</th>
    <th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
    <th style="width:16mm">Base</th>
    <th style="width:24mm">{_tool_hdr_a}</th>
    <th style="width:24mm">{_tool_hdr_b}</th>
  </tr>
  {_power_rows()}
</table>

<!-- PARTICULARS -->
<table>
  <tr>
    <th style="width:6mm">Sr</th>
    <th>Particular</th>
    <th style="width:28mm">R Blank / Batch</th>
    <th style="width:28mm">L Blank / Batch</th>
    <th style="width:8mm">Qty</th>
  </tr>
  <tr>
    <td>1</td>
    <td class="product-name">{product}</td>
    <td style="font-size:6.5pt;text-align:left">{r_blank}{f' [{(R or {{}}).get("batch","")}]' if (R or {{}}).get('batch') else ''}</td>
    <td style="font-size:6.5pt;text-align:left">{l_blank}{f' [{(L or {{}}).get("batch","")}]' if (L or {{}}).get('batch') else ''}</td>
    <td>1</td>
  </tr>
</table>

<!-- BARCODES -->
<div class="barcodes">
  <div class="bc-box"><div class="bc-eye">ORDER</div>{_barcode_img(bc_order)}</div>
  <div class="bc-box"><div class="bc-eye">PARTY / CUSTOMER</div>{_barcode_img(bc_customer)}</div>
</div>

{f'<div style="font-size:6.5pt;margin-top:0.5mm"><span style=\\"font-weight:900\\">Notes:</span> {notes_val}</div>' if notes_val else ''}

</div><!-- end .page -->

<div class="no-print" style="text-align:center;padding:10px">
  <button class="print-btn" onclick="
    document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');
    window.print();
    setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),600);
  ">&#128424; Print Job Card (A5 Landscape)</button>
</div>

</body></html>"""

    _direct_ok = False
    try:
        from modules.settings.shop_master import _get as _shop_flag
        _mode = str(_shop_flag("document_print_mode", "DIRECT_THEN_HTML") or "").upper()
        if _mode in ("DIRECT", "DIRECT_THEN_HTML", "SILENT", "LOCAL"):
            from modules.printing.direct_print import spool_html_to_printer
            _direct_ok, _direct_msg = spool_html_to_printer(
                _print_html,
                job_name=f"job_card_{order_no}",
            )
            if _direct_ok:
                st.success(f"Sent job card to Canon: {_direct_msg}")
            elif _mode == "DIRECT":
                st.warning(_direct_msg)
    except Exception as _dpe:
        _direct_ok = False
        log.debug("Direct job-card print fallback: %s", _dpe)

    if not _direct_ok:
        _b64_html = _b64.b64encode(_print_html.encode("utf-8")).decode()
        _comp.html(
            f"<script>(function(){{"
            f"var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});"
            f"window.open(URL.createObjectURL(b),'_blank');"
            f"}})();</script>",
            height=0
        )

    # Also advance stage to JOB_PRINTED
    for _pl in [ln for ln in [r_line, l_line] if ln]:
        _jm_id = _get_job_master_id(_pl)
        if _jm_id:
            try:
                from modules.sql_adapter import run_scalar
                run_scalar(
                    "SELECT public.advance_job_stage(%(j)s::uuid,'JOB_PRINTED',NULL::uuid)",
                    {"j": _jm_id}
                )
            except Exception:
                pass

    st.success("✅ Job card opened in new tab — click Print in that tab")


def _get_job_master_id(line: dict) -> str | None:
    """Return job_master.id for this order line, or None."""
    try:
        from modules.sql_adapter import run_query
        lid = (line.get("line_id") or line.get("id") or "").strip()
        if not lid:
            return None
        rows = run_query(
            "SELECT id FROM job_master WHERE order_line_id = %(lid)s::uuid LIMIT 1",
            {"lid": lid}
        )
        if rows:
            return str(rows[0]["id"])
    except Exception:
        pass
    return None




# ======================================================
# PAIRED PRINT VIEW — R + L side by side
# ======================================================

def render_job_card_print_pair(r_line: Dict, l_line: Dict, order: Dict):
    """Print both eyes — delegates to _open_jc_print_window."""
    _open_jc_print_window(r_line, l_line, order)


def build_surfacing_data_from_session(line: dict, order: dict) -> Optional[dict]:
    """Build surfacing_data from the visible job-card widgets."""
    import json as _json

    lk = _line_key(line)
    blank = st.session_state.get(_blank_selection_key(line))
    if not blank:
        blank = st.session_state.get(f"selected_blank_{lk}")
    if not isinstance(blank, dict):
        return None

    def _sf(v, default=0.0):
        try:
            if v in (None, "", "None"):
                return default
            return float(v)
        except Exception:
            return default

    def _si(v, default=0):
        try:
            if v in (None, "", "None"):
                return default
            return int(float(v))
        except Exception:
            return default

    def _parse_first_float(v, default=None):
        if isinstance(v, (int, float)):
            return float(v)
        text = str(v or "").strip().replace("+", "")
        for token in text.replace("(", " ").replace(")", " ").split():
            try:
                return float(token)
            except Exception:
                pass
        return default

    lp = line.get("lens_params") or {}
    if isinstance(lp, str):
        try:
            lp = _json.loads(lp)
        except Exception:
            lp = {}
    bp = line.get("boxing_params") or {}
    if isinstance(bp, str):
        try:
            bp = _json.loads(bp)
        except Exception:
            bp = {}

    sph = _sf(line.get("sph"))
    cyl = _sf(line.get("cyl"))
    axis = _si(line.get("axis"))
    eye_side = str(line.get("eye_side") or "").upper().strip()
    add_power = line.get("add_power")
    selected_add = _parse_first_float(st.session_state.get(f"add_{lk}"), _sf(add_power, 0.0))

    raw_category = (
        str(line.get("type") or line.get("category") or line.get("lens_category") or "").strip()
        or "Progressive"
    )

    first_calc = calculate_surfacing_powers(sph, cyl, axis, eye_side, raw_category, base_curve=None)
    system_recommended_base = recommend_base_curve(first_calc["sph_surf"], first_calc["cyl_surf"])
    recommended_base = system_recommended_base
    try:
        inventory_base = round(float(blank.get("base_recommended")), 2)
    except Exception:
        inventory_base = None
    pre_base = st.session_state.get(f"jc_base_pre_{lk}")
    if pre_base is not None and inventory_base is None:
        recommended_base = _sf(pre_base, recommended_base)
    if inventory_base is not None:
        recommended_base = inventory_base

    base_mode = st.session_state.get(f"base_mode_{lk}", "Dropdown")
    if base_mode == "Manual":
        base_curve = round(_sf(st.session_state.get(f"base_manual_{lk}"), recommended_base), 2)
    else:
        base_curve = recommended_base
        labels = get_base_curve_options(blank, recommended_base)
        selected_label = st.session_state.get(f"base_{lk}")
        for opt in labels:
            if opt.get("label") == selected_label:
                base_curve = opt.get("value")
                break

    diameter = st.session_state.get(f"diameter_{lk}", "75mm")
    if diameter == "Custom":
        diameter = f"{_sf(st.session_state.get(f'custom_diameter_{lk}'), 75.0)}mm"

    frame_type = st.session_state.get(f"frame_type_{lk}") or lp.get("frame_type") or "Full Rim"
    edge_finish = st.session_state.get(f"edge_type_{lk}") or "Standard"
    priority = st.session_state.get(f"priority_{lk}") or "Standard (3-5 days)"
    instructions = st.session_state.get(f"instructions_{lk}", lp.get("instructions", ""))
    fitting_height = st.session_state.get(f"fitting_ht_{lk}", lp.get("fitting_height", ""))

    surf_final = calculate_surfacing_powers(sph, cyl, axis, eye_side, raw_category, base_curve)

    return {
        "blank_id": str(blank.get("id") or ""),
        "blank_brand": blank.get("brand", ""),
        "blank_material": blank.get("material", ""),
        "blank_colour": blank.get("colour", ""),
        "blank_batch": blank.get("batch_no", ""),
        "blank_cost_per_pcs": float(blank.get("cost_price") or 0),
        "blank_add": float(blank.get("add_power") or 0),
        "add_power_selected": selected_add,
        "base_curve": base_curve,
        "base_curve_recommended": recommended_base,
        "inventory_base": inventory_base,
        "selected_base": base_curve,
        "recommended_base": inventory_base,
        "system_recommended_base": system_recommended_base,
        "sph_surf": surf_final["sph_surf"],
        "cyl_surf": surf_final["cyl_surf"],
        "axis_surf": surf_final["axis_surf"],
        "tool_a": surf_final.get("tool_a"),
        "tool_b": surf_final.get("tool_b"),
        "kryptok_applied": surf_final.get("kryptok_correction_applied", False),
        "diameter": diameter,
        "frame_type": frame_type,
        "edge_finish": edge_finish,
        "priority": priority,
        "fitting_height": fitting_height,
        "special_instructions": instructions,
    }


def save_job_card_line(line: dict, order: dict) -> tuple[bool, str]:
    """
    Atomic save of a job card line using the DB function
    allocate_blank_and_save_job() which runs everything in one transaction:
        a) FOR UPDATE lock on blank_inventory row
        b) Stock check (eye-specific qty >= 1)
        c) Stock deduction
        d) blank_stock_ledger entry
        e) blank_allocations upsert
        f) order_lines.lens_params update (surfacing_data saved, WIP cleared)
        g) job_master upsert

    Falls back to legacy sequential writes if DB function unavailable.
    Returns (success: bool, message: str).
    """
    import json as _jsc

    surf = line.get("surfacing_data")
    if not surf:
        return False, f"No surfacing data for {line.get('eye_side','?')} eye — fill the job card first"

    eye_side = (line.get("eye_side") or "").upper()
    lid      = (line.get("line_id") or line.get("id") or "").strip()
    if not lid:
        return False, "Missing line_id — cannot save"

    # ── Check if already saved (idempotent re-save path) ─────────────
    # Uses allocation existence as the definitive check — not lens_params read
    # This avoids the TOCTOU race: read says "not saved" but concurrent save wins
    _already_saved = False
    _current_lp    = {}
    try:
        from modules.sql_adapter import run_query as _rq
        _rows = _rq(
            "SELECT ol.lens_params, "
            "    (SELECT 1 FROM blank_allocations ba "
            "     WHERE ba.order_line_id = ol.id LIMIT 1) AS has_allocation "
            "FROM order_lines ol WHERE ol.id = %(l)s::uuid LIMIT 1",
            {"l": lid}
        )
        if _rows:
            _lp = _rows[0].get("lens_params") or {}
            if isinstance(_lp, str):
                try: _lp = _jsc.loads(_lp)
                except Exception as e:
                    log.debug("Could not parse lens_params: %s", e)
                    _lp = {}
            _current_lp   = _lp if isinstance(_lp, dict) else {}
            # Saved = allocation exists AND surfacing_data present
            # Both must be true — allocation without surfacing_data = partial save
            _has_alloc    = bool(_rows[0].get("has_allocation"))
            _has_surf     = bool(_current_lp.get("surfacing_data"))
            _already_saved = _has_alloc and _has_surf
    except Exception:
        pass

    if _already_saved:
        # Re-save path: update surfacing_data only, no inventory touch
        _persist_surfacing_to_db(line)
        _upsert_job_master(line, order)
        return True, f"✅ {eye_side} eye job card updated"

    # ── First save: use atomic DB function ────────────────────────────
    blank_id = str(surf.get("blank_id") or "")
    if not blank_id:
        return False, f"No blank selected for {eye_side} eye"

    # Determine eye-specificity from blank category
    # Progressive and D Bifocal use qty_right/qty_left
    # All others (Kryptok, SV) use qty_independent
    _is_eye_specific = True
    try:
        from modules.sql_adapter import run_query as _rq2
        _b = _rq2(
            "SELECT category FROM blank_inventory WHERE id = %(b)s::uuid LIMIT 1",
            {"b": blank_id}
        )
        if _b:
            _bcat = str(_b[0].get("category") or "").upper()
            _is_eye_specific = any(x in _bcat for x in ("PROGRESSIVE", "D BIFOCAL", "V2", "PAL"))
    except Exception:
        pass

    qty       = int(line.get("billing_qty") or line.get("quantity") or 1)
    base_sel  = surf.get("base_curve")
    surf_json = _jsc.dumps(surf) if isinstance(surf, dict) else str(surf)
    lp_json   = _jsc.dumps(_current_lp) if _current_lp else "{}"

    # ── Try atomic DB function first ──────────────────────────────────
    try:
        from modules.sql_adapter import run_scalar
        result = run_scalar(
            """
            SELECT public.allocate_blank_and_save_job(
                %(lid)s::uuid,
                %(bid)s::uuid,
                %(eye)s,
                %(base)s,
                %(surf)s::jsonb,
                %(lp)s::jsonb,
                %(qty)s,
                NULL::uuid,
                %(eye_spec)s
            )
            """,
            {
                "lid":      lid,
                "bid":      blank_id,
                "eye":      eye_side[:1] if _is_eye_specific else None,
                "base":     float(base_sel) if base_sel else None,
                "surf":     surf_json,
                "lp":       lp_json,
                "qty":      qty,
                "eye_spec": _is_eye_specific,
            }
        )
        if result == "OK":
            return True, f"✅ {eye_side} eye saved & inventory updated (atomic)"
        elif result and str(result).startswith("ERROR"):
            return False, f"❌ {result.replace('ERROR: ', '')}"
        # Unexpected result — fall through to legacy
    except Exception as _fn_err:
        import logging
        logging.getLogger(__name__).warning(
            f"allocate_blank_and_save_job() unavailable — falling back to legacy: {_fn_err}"
        )

    # ── Legacy fallback: Python-level atomic transaction ─────────────
    # Used when allocate_blank_and_save_job() DB function is not yet deployed.
    # Uses get_connection() for explicit transaction control.
    import json as _jsc2
    try:
        from modules.sql_adapter import get_connection as _gc
        _conn = _gc()
        _conn.autocommit = False
        try:
            with _conn.cursor() as _cur:
                # a) Lock blank row exclusively — prevents double allocation
                _cur.execute(
                    "SELECT qty_right, qty_left, qty_independent "
                    "FROM blank_inventory WHERE id = %s FOR UPDATE",
                    (blank_id,)
                )
                _inv = _cur.fetchone()
                if not _inv:
                    raise Exception("Blank not found in inventory")
                _qr, _ql, _qi = (_inv[0] or 0), (_inv[1] or 0), (_inv[2] or 0)

                # b) Check stock for correct eye
                _eye_col = eye_side[:1] if _is_eye_specific else None
                if _is_eye_specific:
                    if _eye_col == "R" and _qr < 1:
                        raise Exception(f"No R eye stock available (qty_right={_qr})")
                    if _eye_col == "L" and _ql < 1:
                        raise Exception(f"No L eye stock available (qty_left={_ql})")
                else:
                    if _qi < 1:
                        raise Exception(f"No independent stock available (qty_independent={_qi})")

                # c) Deduct stock with safety guard
                if _is_eye_specific:
                    if _eye_col == "R":
                        _cur.execute(
                            "UPDATE blank_inventory SET qty_right = qty_right - 1, "
                            "updated_at = NOW() WHERE id = %s AND qty_right >= 1",
                            (blank_id,)
                        )
                    else:
                        _cur.execute(
                            "UPDATE blank_inventory SET qty_left = qty_left - 1, "
                            "updated_at = NOW() WHERE id = %s AND qty_left >= 1",
                            (blank_id,)
                        )
                else:
                    _cur.execute(
                        "UPDATE blank_inventory SET qty_independent = qty_independent - 1, "
                        "updated_at = NOW() WHERE id = %s AND qty_independent >= 1",
                        (blank_id,)
                    )
                if _cur.rowcount == 0:
                    raise Exception("Stock race condition — deduction failed, try again")

                # d) Ledger entry
                _cur.execute(
                    "INSERT INTO blank_stock_ledger "
                    "(blank_id, order_line_id, eye_side, qty_change, ref_type, ref_id, remarks) "
                    "VALUES (%s, %s, %s, -1, 'ALLOCATION', %s, %s)",
                    (blank_id, lid, _eye_col,
                     lid, f"Job card allocation — eye: {_eye_col or 'INDEP'}")
                )

                # e) Upsert blank_allocations
                _base_f = float(base_sel) if base_sel else None
                _cur.execute(
                    "INSERT INTO blank_allocations "
                    "(id, order_line_id, blank_id, eye_side, base_selected, allocated_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (order_line_id) DO UPDATE "
                    "SET blank_id = EXCLUDED.blank_id, eye_side = EXCLUDED.eye_side, "
                    "base_selected = EXCLUDED.base_selected, allocated_at = NOW()",
                    (lid, blank_id, _eye_col, _base_f)
                )

                # f) Merge surfacing_data into lens_params + clear WIP
                _merged = dict(_current_lp)
                _merged["surfacing_data"] = surf
                _merged.pop("job_card_wip", None)
                _cur.execute(
                    "UPDATE order_lines SET lens_params = %s::jsonb "
                    "WHERE id = %s",
                    (_jsc2.dumps(_merged), lid)
                )

                # g) Upsert job_master
                _qty = int(line.get("billing_qty") or line.get("quantity") or 1)
                _cur.execute(
                    "INSERT INTO job_master "
                    "(id, order_line_id, total_qty, blank_required_qty, blank_allocated_qty, "
                    "current_stage, reprocess_count, is_closed, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, 'JOB_CREATED', 0, FALSE, NOW(), NOW()) "
                    "ON CONFLICT (order_line_id) DO UPDATE "
                    "SET blank_allocated_qty = EXCLUDED.blank_allocated_qty, updated_at = NOW()",
                    (lid, _qty, _qty, _qty)
                )

            _conn.commit()
            return True, f"✅ {eye_side} eye saved & inventory updated"

        except Exception as _tx_err:
            _conn.rollback()
            return False, f"❌ {str(_tx_err)}"
        finally:
            try: _conn.autocommit = True
            except Exception: pass

    except ImportError:
        pass  # get_connection not available — last resort below

    # ── Last resort: old sequential writes (no get_connection available) ─
    success = update_blank_quantity(
        blank_id=blank_id,
        qty_change=-1,
        eye_side=eye_side if _is_eye_specific else None
    )
    if not success:
        return False, f"❌ Inventory update failed for {eye_side} eye — check availability"
    _persist_surfacing_to_db(line)
    _upsert_job_master(line, order)
    return True, f"✅ {eye_side} eye saved (⚠ non-atomic — deploy migration SQL)"


def _render_label_for_job_card(line: dict, order: dict):
    """Render barcode label for a single job card line."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    _ono = order.get("order_no", "—")
    _pat = order.get("patient_name", "—")
    _eye = (line.get("eye_side") or "").upper()
    _eye_lbl = "RIGHT" if _eye in ("R","RIGHT") else "LEFT" if _eye in ("L","LEFT") else _eye
    
    _sph = line.get("sph", 0) or 0
    _cyl = line.get("cyl", 0) or 0
    _ax = int(line.get("axis") or 0)
    _add = line.get("add_power") or 0
    
    def _fp(v):
        try:
            n = float(v or 0)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception as e:
            log.debug("Job-card display fallback: %s", e)
        return "—"

    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:75mm 50mm;margin:0}}body{{margin:0;font-family:Arial}}
    .lbl{{width:75mm;height:50mm;box-sizing:border-box;padding:3mm 4mm;background:#fff;border:.5mm solid #333}}
    .ln{{font-size:11pt;font-weight:700;color:#0f172a;margin-bottom:1mm}}
    .lm{{font-size:7pt;color:#475569;margin-bottom:2mm;font-family:monospace}}
    table{{border-collapse:collapse;width:100%;font-size:8pt}}
    th{{background:#0f172a;color:#fff;padding:1.5mm 2mm;text-align:center;font-size:7pt}}
    td{{padding:1.5mm 2mm;text-align:center;border-bottom:.3mm solid #e2e8f0;color:#0f172a}}
    td.le{{color:#64748b;font-weight:700;text-align:left}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>
    <div class='lbl'>
        <div class='ln'>{_pat}</div>
        <div class='lm'>{_ono} · {_eye_lbl}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
        <tr><td class='le'>{_eye[:1]}</td><td>{_fp(_sph)}</td><td>{_fp(_cyl)}</td><td>{_ax}</td><td>{_fp(_add)}</td></tr>
        </table>
    </div>
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#0f172a;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>🖨️ Print Label</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success("✅ Label print dialog opened")


def _render_cr80_for_job_card(line: dict, order: dict):
    """Render CR80 authenticity card for a single job card line."""
    import streamlit.components.v1 as _comp
    import base64 as _b64
    
    _ono = order.get("order_no", "—")
    _pat = order.get("patient_name", "—")
    _party = order.get("party_name", "")
    _eye = (line.get("eye_side") or "").upper()
    _eye_lbl = "RIGHT" if _eye in ("R","RIGHT") else "LEFT" if _eye in ("L","LEFT") else _eye
    
    _sph = line.get("sph", 0) or 0
    _cyl = line.get("cyl", 0) or 0
    _ax = int(line.get("axis") or 0)
    _add = line.get("add_power") or 0
    
    def _fp(v):
        try:
            n = float(v or 0)
            return f"{'+' if n >= 0 else ''}{n:.2f}"
        except Exception as e:
            log.debug("Job-card display fallback: %s", e)
        return "—"

    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:85.6mm 54mm;margin:0}}body{{margin:0;font-family:Arial}}
    .card{{width:85.6mm;height:54mm;box-sizing:border-box;padding:4mm 5mm;
           background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;position:relative}}
    .logo{{position:absolute;top:4mm;right:5mm;font-size:18pt;opacity:.5}}
    .badge{{font-size:5.5pt;letter-spacing:.12em;text-transform:uppercase;color:#a78bfa;font-weight:700;margin-bottom:2mm}}
    .name{{font-size:11pt;font-weight:700;margin-bottom:1.5mm}}
    .mobile{{font-size:8pt;color:#94a3b8;margin-bottom:2.5mm}}
    table{{border-collapse:collapse;width:100%;font-size:7pt}}
    th{{background:rgba(255,255,255,.1);color:#94a3b8;padding:1mm 1.5mm;text-align:center;font-weight:600}}
    td{{color:#e2e8f0;padding:1mm 1.5mm;text-align:center;border-bottom:.3mm solid rgba(255,255,255,.08)}}
    td.lbl{{color:#64748b;text-align:left}}
    .footer{{position:absolute;bottom:3mm;left:5mm;right:5mm;display:flex;justify-content:space-between}}
    .ono{{font-family:monospace;font-size:7pt;color:#475569}}.dealer{{font-size:6.5pt;color:#334155}}
    .no-print{{display:none}}@media print{{.no-print{{display:none!important}}}}
    </style></head><body>
    <div class='card'>
        <div class='logo'>👁️</div>
        <div class='badge'>AUTHENTICITY CARD</div>
        <div class='name'>{_pat}</div>
        <div class='mobile'>{_ono} · {_eye_lbl}</div>
        <table><tr><th></th><th>SPH</th><th>CYL</th><th>AX</th><th>ADD</th></tr>
        <tr><td class='lbl'>{_eye[:1]}</td><td>{_fp(_sph)}</td><td>{_fp(_cyl)}</td><td>{_ax}</td><td>{_fp(_add)}</td></tr>
        </table>
        <div class='footer'><span class='ono'>{_ono}</span><span class='dealer'>{_party}</span></div>
    </div>
    <div class='no-print' style='text-align:center;padding:20px'>
    <button onclick='window.print()' style='background:#6366f1;color:#fff;border:none;
    padding:10px 32px;border-radius:8px;font-weight:700;cursor:pointer'>🖨️ Print CR80 Card</button>
    </div></body></html>"""
    _b64_html = _b64.b64encode(_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{var _raw=atob('{_b64_html}');var _buf=new Uint8Array(_raw.length);for(var _i=0;_i<_raw.length;_i++){{_buf[_i]=_raw.charCodeAt(_i);}}var b=new Blob([_buf],{{type:'text/html;charset=utf-8'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
        height=0
    )
    st.success("✅ CR80 print dialog opened")


# ======================================================
# EXPORTS
# ======================================================

__all__ = [
    'render_surfacing_job_card',
    'render_job_card_print',
    'render_job_card_print_pair',
    '_open_jc_print_window',
    'save_job_card_line',
    'build_surfacing_data_from_session',
]
