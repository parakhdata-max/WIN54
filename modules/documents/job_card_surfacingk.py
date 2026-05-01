"""
Job Card Generator for Surfacing Operations - ENHANCED VERSION
Flow: Category → Material → Add → Brand → Qty → Selection → Base Curve → Parameters → Calculations
"""

import streamlit as st
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

    Uses line_id (UUID) + eye_side — both are immutable for a given
    order line.  Falls back to order_no + eye_side if line_id is absent
    (older orders).  NEVER uses id(line) (Python memory address).
    """
    lid  = (line.get("line_id") or line.get("id") or "").strip()
    eye  = (line.get("eye_side") or "X").upper().strip()
    ono  = (line.get("order_no") or "").strip()
    if lid:
        return f"jc_{lid}_{eye}"
    return f"jc_{ono}_{eye}"


def _pair_key(line: dict) -> str:
    """
    Shared key for R+L pair — same product, same order, NO eye side.
    Used for blank selection so one click selects for both eyes.
    """
    pid = (line.get("product_id") or line.get("product_name") or "unk").strip()
    ono = (line.get("order_no") or "").strip()
    lid = (line.get("line_id") or line.get("id") or "").strip()
    # Use first 8 chars of line_id as order anchor (strips eye suffix)
    return f"jc_pair_{pid}_{ono}_{lid[:8]}"


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
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb, updated_at = NOW() "
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
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb, updated_at = NOW() "
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
            "UPDATE order_lines SET lens_params = %(lp)s::jsonb, updated_at = NOW() "
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
        import logging
        logging.getLogger(__name__).warning(f"_persist_surfacing_to_db failed: {_e}")


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
        except:
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
    is_eye_specific = any(x in category.lower() for x in ["progressive", "d bifocal"])
    
    def qty_ok(row):
        if is_eye_specific:
            if eye_side == "R":
                return row["qty_right"] > 0
            else:
                return row["qty_left"] > 0
        return row["qty_independent"] > 0
    
    blanks = blanks[blanks.apply(qty_ok, axis=1)]
    
    return blanks

# ======================================================
# MAIN UI - ENHANCED FLOW
# ======================================================

def render_surfacing_job_card(line: Dict, order: Dict):
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

    _LOCKED_STAGES = {"PRINTED", "PRODUCTION_PICKED", "PRODUCTION_DONE",
                      "INSPECTION", "HARDCOAT_PICKED", "HARDCOAT_DONE",
                      "COLOURING_PICKED", "COLOURING_DONE", "ARC_SENT",
                      "ARC_RECEIVED", "FINAL_QC", "READY_FOR_PACK",
                      "FITTING_PENDING", "FITTING_SENT", "FITTING_RECEIVED",
                      "FITTING_DONE", "DISPATCHED", "DELIVERED"}

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
                except: return 0.0
            st.info(
                f"**RX:** SPH {_sf(line.get('sph')):+.2f} / CYL {_sf(line.get('cyl')):+.2f} / AXIS {int(_sf(line.get('axis')))}°  |  "
                f"**SURF:** SPH {_sf(surf.get('sph_surf')):+.2f} / CYL {_sf(surf.get('cyl_surf')):+.2f}  |  "
                f"Dia: {surf.get('diameter','—')}  |  Frame: {surf.get('frame_type','—')}"
            )
        return  # ← nothing else rendered

    # ── In-progress status banner ──────────────────────────────
    _lk_banner = _line_key(line)
    _blank_ss_key_banner = f"selected_blank_{_lk_banner}"
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

    _cat_col   = (line.get("category")     or "").strip()
    lens_category_raw = (line.get("lens_category") or "").strip()
    _main_grp  = (line.get("main_group")   or "").strip()

    # Pick most specific non-generic value
    # Treat "Ophthalmic Lenses" / "Ophthalmic Lens" as generic (skip them)
    _GENERIC = {"OPHTHALMIC LENSES", "OPHTHALMIC LENS"}
    raw_category = (
        _cat_col       if _cat_col.upper()   not in _GENERIC else
        lens_category_raw if lens_category_raw else
        _main_grp
    )
    if not raw_category:
        raw_category = _cat_col or _main_grp

    # Map product master values → blank_inventory category values.
    # blank_inventory.category uses: "Single Vision", "Progressive", "Bifocal",
    #   "Kryptok", "D Bifocal", "Toric", "Reading"  (actual DB values).
    _CATEGORY_MAP = {
        # ── Single Vision ────────────────────────────────────────────
        "SINGLE VISION":        "Single Vision",
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
            _blank_ss_key = f"selected_blank_{_lk}"
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
    st.info(f"**Category:** {_display_cat} (from product master)")
    
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
    
    selected_material = st.selectbox(
        "Material",
        available_materials,
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
    # STEP 4: BRAND FILTER
    # ==================================================
    available_brands = get_available_brands(category, selected_material, selected_add)
    
    if not available_brands:
        st.warning(f"⚠️ No brands available for {selected_material} with add {selected_add}")
        return
    
    selected_brand = st.selectbox(
        "Brand",
        available_brands,
        key=f"brand_{_line_key(line)}"
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
    # STEP 5: GET FILTERED BLANKS
    # ==================================================
    filtered_blanks = filter_blanks_by_selection(
        category=category,
        material=selected_material,
        add_power=selected_add,
        brand=selected_brand,
        colour=selected_colour,
        eye_side=eye_side
    )
    
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
            key=f"blank_scan_{_pair_key(line)}",
            label_visibility="collapsed",
        ).strip().upper()
        if _scanned:
            st.session_state[f"blank_scan_val_{_pair_key(line)}"] = _scanned
    with _clear_col:
        if st.button("✕", key=f"blank_scan_clear_{_pair_key(line)}", use_container_width=True,
                     help="Clear scanner input"):
            st.session_state.pop(f"blank_scan_val_{_pair_key(line)}", None)
            st.session_state.pop(f"blank_scan_{_pair_key(line)}", None)
            st.rerun()

    _scan_val = st.session_state.get(f"blank_scan_val_{_pair_key(line)}", "")
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
            _sel_key = f"selected_blank_{_pair_key(line)}"
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

        checkbox_key = f"select_blank_{blank_row['id']}_{_pair_key(line)}"

        # Determine if already selected — SHARED key (no eye side) so R and L share selection
        _sel_key = f"selected_blank_{_pair_key(line)}"
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
                qty_r = int(blank_row["qty_right"]) if pd.notna(blank_row.get("qty_right")) else 0
                color_r = "#4ade80" if qty_r >= 5 else ("#f59e0b" if qty_r > 0 else "#ef4444")
                last_r  = "<div style='font-size:0.6rem;color:#f59e0b;font-weight:700'>⚠️ LAST 1</div>" if qty_r == 1 else ""
                st.markdown(
                    f"<div style='text-align:center;'>"
                    f"<div style='font-size:0.7rem;color:#94a3b8;'>👁 R</div>"
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
                        # Deselect any previously selected blank for this line
                        # Write to shared key — both R and L will read same selection
                        st.session_state[_sel_key] = _selected_blank_dict
                        # Also mirror to both R/L individual keys so _save_jc_progress works
                        _save_jc_progress(line, {"blank_row": _selected_blank_dict})
                        # No st.rerun() — selection registers immediately via session_state

    
    # ==================================================
    # STEP 8: PROCESS SELECTED BLANK
    # ==================================================
    selected_blank_key = f"selected_blank_{_pair_key(line)}"  # shared R+L key

    
    if selected_blank_key not in st.session_state:
        st.info("👆 Please select a blank to continue")
        return
    
    blank = st.session_state[selected_blank_key]
    
    st.success(f"✅ Selected: {blank['brand']} {blank['material']} (Batch: {blank['batch_no']})")
    
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

    # System calculated base
    recommended_base = recommend_base_curve(
        surf_calc["sph_surf"],
        surf_calc["cyl_surf"]
    )

    st.caption(
        f"💡 System Recommended: **{recommended_base:.2f}D**"
    )

    # Build options from DB + System
    base_options = get_base_curve_options(blank, recommended_base)

    if not base_options:
        st.error("❌ No base curves available for this blank")
        return

    # Labels
    base_labels = [opt["label"] for opt in base_options]

    # Default = recommended
    default_idx = 0
    for i, opt in enumerate(base_options):
        if opt["is_recommended"]:
            default_idx = i
            break

    # --- Input mode toggle ---
    _base_mode_key = f"base_mode_{_line_key(line)}"
    base_input_mode = st.radio(
        "Base Curve Input",
        ["Dropdown", "Manual"],
        index=0,
        horizontal=True,
        key=_base_mode_key,
    )

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
            value=float(recommended_base),
            step=0.25,
            format="%.2f",
            key=_manual_key,
        )
        base_curve = round(float(manual_val), 2)

    st.success(
        f"✅ Selected Base Curve: **{base_curve:.2f}D**"
    )

    st.markdown("---")
    
    # ==================================================
    # STEP 10: LENS PARAMETERS — pre-filled from lens_params
    # ==================================================
    import json as _lpj3
    _lp3 = line.get("lens_params") or {}
    if isinstance(_lp3, str):
        try: _lp3 = _lpj3.loads(_lp3)
        except: _lp3 = {}

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
    _frame_prefill = _frame_map.get(_lp_frame.lower(), "Full Rim")
    if _frame_prefill not in _FRAME_OPTIONS:
        _frame_prefill = "Full Rim"
    _frame_idx = _FRAME_OPTIONS.index(_frame_prefill)

    # Diameter from lens_params or boxing_params
    _DIAM_OPTIONS = ["65mm", "70mm", "75mm", "80mm", "Custom"]
    _bp3 = line.get("boxing_params") or {}
    if isinstance(_bp3, str):
        try: _bp3 = _lpj3.loads(_bp3)
        except: _bp3 = {}
    _ed_raw = str(_bp3.get("ed") or _lp_diam or "").strip()
    _diam_prefill = "75mm"
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
    _diam_idx = _DIAM_OPTIONS.index(_diam_prefill) if _diam_prefill in _DIAM_OPTIONS else 2

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
        _edge_prefill = _edge_map.get(_lp_thick.lower(), "Standard")
        if _edge_prefill not in _EDGE_OPTIONS: _edge_prefill = "Standard"
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
    # SAVE JOB CARD
    # ==================================================
    st.markdown("---")
    
    col_save, col_print = st.columns(2)
    
    with col_save:
        if st.button(
            "💾 Save Job Card & Update Inventory",
            type="primary",
            key=f"save_{_line_key(line)}"
        ):
    
            # Store complete surfacing data in line
            line["surfacing_data"] = {
                # Blank info
                "blank_id": str(blank["id"]),
                "blank_brand": blank["brand"],
                "blank_material": blank["material"],
                "blank_colour": blank["colour"],
                "blank_batch": blank["batch_no"],
                "add_power_selected": selected_add,
                
                # Base curve
                "base_curve": base_curve,
                "base_curve_recommended": recommended_base,
                
                # Surfacing powers
                "sph_surf": surf_final["sph_surf"],
                "cyl_surf": surf_final["cyl_surf"],
                "axis_surf": surf_final["axis_surf"],
                
                # Tools
                "tool_a": surf_final.get("tool_a"),
                "tool_b": surf_final.get("tool_b"),
                
                # Corrections
                "kryptok_applied": surf_final.get("kryptok_correction_applied", False),
                
                # Processing parameters
                "diameter": final_diameter,
                "frame_type": selected_frame_type,
                "edge_finish": selected_edge_type,
                "priority": selected_priority,
                "special_instructions": special_instructions,
            }
            
            # ── DOUBLE-SAVE GUARD ───────────────────────────────────────
            # Streamlit can fire the save block twice on fast reruns.
            _save_guard_key = f"jc_saving_{_line_key(line)}"
            if st.session_state.get(_save_guard_key):
                st.stop()
            st.session_state[_save_guard_key] = True

            # ── INVENTORY DEDUCTION GUARD ───────────────────────────────
            # Only deduct inventory if this line does NOT already have a
            # saved job card in DB. Re-saving an existing job card must NOT
            # deduct inventory again — blank was already consumed on first save.
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

            if _already_saved:
                # Job card already saved — update surfacing data + blank_allocations
                # but DO NOT touch blank_inventory quantities again
                line["surfacing_data"] = surfacing_data
                _persist_surfacing_to_db(line)
                _upsert_job_master(line, order)
                st.success("✅ Job card updated successfully!")
                if selected_blank_key in st.session_state:
                    del st.session_state[selected_blank_key]
                st.session_state.pop(f"jc_saving_{_line_key(line)}", None)
                st.rerun()

            # ── First-time save: deduct inventory ──────────────────────
            success = update_blank_quantity(
                blank_id=str(blank["id"]),
                qty_change=-1,
                eye_side=eye_side if is_eye_specific else None
            )
            
            if success:
                # ── PERSIST surfacing_data to order_lines.lens_params ──────
                # NOTE: _persist_surfacing_to_db already strips job_card_wip
                # DO NOT call _clear_jc_progress after this — it re-reads DB
                # and overwrites surfacing_data with stale empty lens_params.
                _persist_surfacing_to_db(line)

                # ── CREATE / UPDATE job_master row ─────────────────────────
                _upsert_job_master(line, order)

                st.success("✅ Job card saved & inventory updated!")
                # Clear WIP state
                if selected_blank_key in st.session_state:
                    del st.session_state[selected_blank_key]
                _wip_ss_key2 = f"jc_wip_loaded_{_line_key(line)}"
                if _wip_ss_key2 in st.session_state:
                    del st.session_state[_wip_ss_key2]
                st.session_state.pop(f"jc_saving_{_line_key(line)}", None)
                # No st.rerun() — stay on page so both R and L can be saved
            else:
                st.error("❌ Failed to update inventory. Please check quantity availability.")
                st.session_state.pop(f"jc_saving_{_line_key(line)}", None)
    
    with col_print:
        if st.button("🖨️ Generate Print View", key=f"print_{_line_key(line)}", 
                    use_container_width=True):
            st.session_state[f"show_print_{_line_key(line)}"] = True
            st.rerun()


# ======================================================
# PRINT VIEW
# ======================================================

def render_job_card_print(line: Dict, order: Dict):
    """
    Minimal job card print view.
    Shows: Order no, Patient, Eye, RX, Blank type, Dia, Frame type + Barcode.
    Auto-triggers window.print() and advances stage to PRINTED.
    """
    surf = line.get("surfacing_data")
    if not surf:
        st.warning("⚠️ No surfacing data — save the job card first.")
        return

    import math as _m
    def _pf(v):
        try:
            f = float(v or 0)
            return 0.0 if _m.isnan(f) else f
        except Exception:
            return 0.0
    def _pi(v): return int(_pf(v))

    # ── Key data ─────────────────────────────────────────────────────
    order_no   = order.get("order_no", "—")
    patient    = order.get("patient_name", "—")
    eye        = (line.get("eye_side") or "").upper()
    eye_label  = "RIGHT EYE" if eye in ("R", "RIGHT") else "LEFT EYE"
    line_id    = (line.get("line_id") or line.get("id") or "")[:8].upper()

    sph_rx  = _pf(line.get("sph"));  cyl_rx  = _pf(line.get("cyl"))
    axis_rx = _pi(line.get("axis")); add_rx  = _pf(line.get("add_power"))
    sph_s   = _pf(surf.get("sph_surf")); cyl_s = _pf(surf.get("cyl_surf"))
    axis_s  = _pi(surf.get("axis_surf"))

    brand    = surf.get("blank_brand", "—")
    material = surf.get("blank_material", "—")
    colour   = surf.get("blank_colour") or "Clear"
    base     = _pf(surf.get("base_curve"))
    dia      = surf.get("diameter", "—")
    frame_t  = surf.get("frame_type", "—")
    priority = surf.get("priority", "Standard")
    batch    = surf.get("blank_batch", "—")

    # ── Barcode value = order_no + eye initial ────────────────────────
    barcode_val = f"{order_no}-{eye[0] if eye else 'X'}"

    # ── Auto-print CSS — hides everything except #jc-print ───────────
    st.markdown(f"""
    <style>
    @media print {{
        body > * {{ display: none !important; }}
        #jc-print, #jc-print * {{ display: block !important; visibility: visible !important; }}
        #jc-print {{ position: fixed; top:0; left:0; width:100%; padding:10mm; font-family:monospace; }}
    }}
    </style>
    <div id="jc-print" style="border:2px solid #000;padding:12px;max-width:520px;
         font-family:monospace;font-size:13px;background:#fff;color:#000;">

      <!-- Header -->
      <div style="display:flex;justify-content:space-between;border-bottom:2px solid #000;padding-bottom:6px;margin-bottom:8px">
        <div>
          <div style="font-size:16px;font-weight:900">JOB CARD</div>
          <div style="font-size:11px">Order: <b>{order_no}</b></div>
          <div style="font-size:11px">Patient: <b>{patient}</b></div>
          <div style="font-size:11px">Priority: <b>{priority}</b></div>
        </div>
        <div style="text-align:right">
          <div style="font-size:18px;font-weight:900;background:#{'#1a3a5c' if eye[0:1]=='R' else '#1a3a2a'};
               color:#fff;padding:4px 12px;border-radius:4px">{eye_label}</div>
          <div style="font-size:10px;margin-top:4px">Batch: {batch}</div>
          <div style="font-size:10px">ID: {line_id}</div>
        </div>
      </div>

      <!-- RX row -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:8px">
        <tr style="background:#f0f0f0">
          <th style="border:1px solid #999;padding:3px 6px;font-size:11px"></th>
          <th style="border:1px solid #999;padding:3px 6px;font-size:11px">SPH</th>
          <th style="border:1px solid #999;padding:3px 6px;font-size:11px">CYL</th>
          <th style="border:1px solid #999;padding:3px 6px;font-size:11px">AXIS</th>
          <th style="border:1px solid #999;padding:3px 6px;font-size:11px">ADD</th>
        </tr>
        <tr>
          <td style="border:1px solid #999;padding:3px 6px;font-size:11px;font-weight:700">RX</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{sph_rx:+.2f}</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{cyl_rx:+.2f}</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{axis_rx}°</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{add_rx:+.2f}</td>
        </tr>
        <tr>
          <td style="border:1px solid #999;padding:3px 6px;font-size:11px;font-weight:700">SURF</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{sph_s:+.2f}</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{cyl_s:+.2f}</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">{axis_s}°</td>
          <td style="border:1px solid #999;padding:3px 6px;font-size:12px">—</td>
        </tr>
      </table>

      <!-- Blank + Params row -->
      <div style="display:flex;gap:12px;margin-bottom:8px">
        <div style="flex:1;border:1px solid #999;padding:6px">
          <div style="font-weight:700;font-size:11px;border-bottom:1px solid #ccc;margin-bottom:4px">BLANK</div>
          <div style="font-size:12px"><b>{brand}</b> {material}</div>
          <div style="font-size:11px">Colour: {colour}</div>
          <div style="font-size:11px">Base: <b>{base:.2f}D</b></div>
        </div>
        <div style="flex:1;border:1px solid #999;padding:6px">
          <div style="font-weight:700;font-size:11px;border-bottom:1px solid #ccc;margin-bottom:4px">PARAMS</div>
          <div style="font-size:12px">Dia: <b>{dia}</b></div>
          <div style="font-size:11px">Frame: {frame_t}</div>
        </div>
      </div>

      <!-- Barcode strip -->
      <div style="border:1px solid #999;padding:6px;text-align:center">
        <div style="font-size:10px;letter-spacing:6px;font-weight:900;
             border:1px solid #000;padding:4px;display:inline-block;
             background:#fff;font-family:monospace">{barcode_val}</div>
        <div style="font-size:9px;margin-top:2px;color:#555">{barcode_val}</div>
      </div>

      <!-- Sign-off line -->
      <div style="display:flex;justify-content:space-between;margin-top:10px;font-size:10px">
        <div>Tech: _________________</div>
        <div>QC: __________________</div>
        <div>Date: ________________</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Buttons ───────────────────────────────────────────────────────
    col_print, col_close = st.columns(2)

    with col_print:
        if st.button("🖨️ Print Job Card", type="primary", use_container_width=True,
                     key=f"print_jc_{_line_key(line)}"):
            # Advance stage to PRINTED if still at JOB_CREATED
            _jm_id = _get_job_master_id(line)
            if _jm_id:
                try:
                    from modules.sql_adapter import run_scalar
                    run_scalar(
                        "SELECT public.advance_job_stage(%(j)s::uuid, 'PRINTED', NULL::uuid)",
                        {"j": _jm_id}
                    )
                except Exception:
                    pass  # stage advance is best-effort; print still works
            # Trigger browser print
            st.components.v1.html(
                "<script>window.print();</script>",
                height=0
            )

    with col_close:
        if st.button("✕ Close", use_container_width=True,
                     key=f"close_print_{_line_key(line)}"):
            st.session_state.pop(f"show_print_{line.get('line_id')}", None)
            st.rerun()


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
    """
    Print-optimised job card showing R and L side by side.
    Primary focus: PRODUCT, CATEGORY, CYL (large), TOOL, DIAMETER.
    """
    import math as _m
    def _pf(v):
        try:
            f = float(v or 0)
            return 0.0 if _m.isnan(f) else f
        except Exception: return 0.0
    def _pi(v): return int(_pf(v))
    def _sgn(v):
        f = _pf(v)
        return f"+{f:.2f}" if f >= 0 else f"{f:.2f}"

    order_no  = order.get("order_no", "—")
    patient   = order.get("patient_name", "—")
    prod_name = r_line.get("product_name") or l_line.get("product_name") or "—"
    category  = r_line.get("main_group") or r_line.get("category") or "—"
    brand     = r_line.get("brand") or "—"

    def _eye_data(line):
        surf = line.get("surfacing_data") or {}
        return {
            "sph_rx":  _pf(line.get("sph")),
            "cyl_rx":  _pf(line.get("cyl")),
            "axis_rx": _pi(line.get("axis")),
            "add_rx":  _pf(line.get("add_power")),
            "sph_s":   _pf(surf.get("sph_surf")),
            "cyl_s":   _pf(surf.get("cyl_surf")),
            "axis_s":  _pi(surf.get("axis_surf")),
            "tool_a":  str(surf.get("tool_a") or "—"),
            "tool_b":  str(surf.get("tool_b") or "—"),
            "base":    _pf(surf.get("base_curve")),
            "dia":     str(surf.get("diameter") or "—"),
            "frame":   str(surf.get("frame_type") or "—"),
            "blank":   f"{surf.get('blank_brand','—')} {surf.get('blank_material','')}".strip(),
            "colour":  str(surf.get("blank_colour") or "Clear"),
            "batch":   str(surf.get("blank_batch") or "—"),
            "priority":str(surf.get("priority") or "Standard"),
            "edge":    str(surf.get("edge_finish") or "Standard"),
            "notes":   str(surf.get("special_instructions") or ""),
            "line_id": (line.get("line_id") or line.get("id") or "")[:8].upper(),
            "saved":   bool(surf),
        }

    R = _eye_data(r_line)
    L = _eye_data(l_line)

    import datetime as _dt
    today = _dt.date.today().strftime("%d/%m/%Y")

    # Build one-eye cell HTML
    def _cell(d, eye_label, eye_color, eye_bg):
        _na = "<span style='color:#aaa'>—</span>"
        _cyl_disp = _sgn(d['cyl_s']) if d['saved'] else _na
        _cyl_size = "2.4rem" if d['saved'] else "1.2rem"
        return f"""
        <td style="width:50%;vertical-align:top;padding:0 6px 0 0">
          <div style="border:2px solid {eye_color};border-radius:8px;overflow:hidden">

            <!-- Eye header -->
            <div style="background:{eye_bg};color:{eye_color};font-size:1rem;font-weight:900;
                 padding:6px 12px;letter-spacing:.05em">
              👁 {eye_label}&nbsp;&nbsp;
              <span style="font-size:0.7rem;font-weight:400;color:#94a3b8">ID: {d['line_id']}</span>
            </div>

            <!-- RX powers -->
            <div style="padding:8px 12px;border-bottom:1px solid #1e293b">
              <div style="color:#64748b;font-size:0.62rem;font-weight:700;letter-spacing:.1em;
                   margin-bottom:4px">PRESCRIPTION (RX)</div>
              <table style="width:100%;border-collapse:collapse;font-family:monospace">
                <tr style="color:#94a3b8;font-size:0.65rem">
                  <td style="padding:2px 4px">SPH</td>
                  <td style="padding:2px 4px">CYL</td>
                  <td style="padding:2px 4px">AXIS</td>
                  <td style="padding:2px 4px">ADD</td>
                </tr>
                <tr style="font-size:1rem;font-weight:700;color:#e2e8f0">
                  <td style="padding:2px 4px">{_sgn(d['sph_rx'])}</td>
                  <td style="padding:2px 4px">{_sgn(d['cyl_rx'])}</td>
                  <td style="padding:2px 4px">{d['axis_rx']}°</td>
                  <td style="padding:2px 4px">{"+" + f"{d['add_rx']:.2f}" if d['add_rx'] else "—"}</td>
                </tr>
              </table>
            </div>

            <!-- SURFACING POWERS — CYL enlarged -->
            <div style="padding:8px 12px;border-bottom:1px solid #1e293b;background:#080f1a">
              <div style="color:#64748b;font-size:0.62rem;font-weight:700;letter-spacing:.1em;
                   margin-bottom:6px">SURFACING POWERS</div>
              <div style="display:flex;gap:8px;align-items:flex-end">
                <div style="flex:1;text-align:center">
                  <div style="color:#94a3b8;font-size:0.6rem">SPH</div>
                  <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700;font-family:monospace">
                    {_sgn(d['sph_s']) if d['saved'] else _na}</div>
                </div>
                <!-- CYL — PRIMARY FOCUS -->
                <div style="flex:1.5;text-align:center;background:#1a0a2e;border:2px solid #7c3aed;
                     border-radius:8px;padding:6px 4px">
                  <div style="color:#a78bfa;font-size:0.65rem;font-weight:700">CYL</div>
                  <div style="color:#c4b5fd;font-size:{_cyl_size};font-weight:900;
                       font-family:monospace;line-height:1">{_cyl_disp}</div>
                  <div style="color:#7c3aed;font-size:0.6rem;margin-top:2px">
                    AXIS {d['axis_s'] if d['saved'] else '—'}°</div>
                </div>
                <div style="flex:1;text-align:center">
                  <div style="color:#94a3b8;font-size:0.6rem">BASE</div>
                  <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700;font-family:monospace">
                    {f"{d['base']:.2f}D" if d['saved'] else _na}</div>
                </div>
              </div>
            </div>

            <!-- TOOL + DIAMETER — PRIMARY FOCUS -->
            <div style="padding:8px 12px;border-bottom:1px solid #1e293b;background:#0a1628">
              <div style="color:#64748b;font-size:0.62rem;font-weight:700;letter-spacing:.1em;
                   margin-bottom:6px">TOOL &amp; DIAMETER</div>
              <div style="display:flex;gap:6px">
                <div style="flex:1;background:#0f2040;border:1px solid #1e40af;border-radius:6px;
                     padding:6px;text-align:center">
                  <div style="color:#93c5fd;font-size:0.6rem">TOOL A</div>
                  <div style="color:#dbeafe;font-size:1.4rem;font-weight:900;font-family:monospace">
                    {d['tool_a'] if d['saved'] else _na}</div>
                </div>
                <div style="flex:1;background:#0f2040;border:1px solid #1e40af;border-radius:6px;
                     padding:6px;text-align:center">
                  <div style="color:#93c5fd;font-size:0.6rem">TOOL B</div>
                  <div style="color:#dbeafe;font-size:1.4rem;font-weight:900;font-family:monospace">
                    {d['tool_b'] if d['saved'] else _na}</div>
                </div>
                <div style="flex:1;background:#0d2010;border:1px solid #166534;border-radius:6px;
                     padding:6px;text-align:center">
                  <div style="color:#86efac;font-size:0.6rem">DIAMETER</div>
                  <div style="color:#bbf7d0;font-size:1.4rem;font-weight:900;font-family:monospace">
                    {d['dia'] if d['saved'] else _na}</div>
                </div>
              </div>
            </div>

            <!-- Blank + Frame -->
            <div style="padding:8px 12px">
              <div style="display:flex;justify-content:space-between;
                   font-size:0.75rem;color:#94a3b8;margin-bottom:2px">
                <span>Blank: <b style="color:#e2e8f0">{d['blank']}</b></span>
                <span>Colour: <b style="color:#e2e8f0">{d['colour']}</b></span>
              </div>
              <div style="display:flex;justify-content:space-between;
                   font-size:0.75rem;color:#94a3b8">
                <span>Frame: <b style="color:#e2e8f0">{d['frame']}</b></span>
                <span>Edge: <b style="color:#e2e8f0">{d['edge']}</b></span>
              </div>
              {'<div style="background:#1a1200;border-left:3px solid #f59e0b;padding:4px 8px;' +
               'margin-top:6px;border-radius:0 4px 4px 0;font-size:0.72rem;color:#fcd34d">' +
               '📝 ' + d['notes'] + '</div>' if d['notes'] else ''}
            </div>

            <!-- Sign-off -->
            <div style="border-top:1px solid #1e293b;padding:6px 12px;
                 display:flex;justify-content:space-between;font-size:0.65rem;color:#475569">
              <span>Tech: __________</span>
              <span>QC: ___________</span>
            </div>
          </div>
        </td>"""

    r_cell = _cell(R, "RIGHT EYE", "#4ade80", "#0d2818")
    l_cell = _cell(L, "LEFT EYE",  "#60a5fa", "#0d1f2e")

    # ── Build full printable HTML for iframe ─────────────────────────
    _print_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
         background: #fff; color: #000; padding: 10px; }}

  .header {{ display:flex; justify-content:space-between; align-items:center;
             border-bottom: 2px solid #000; padding-bottom: 8px; margin-bottom: 10px; }}
  .prod-name {{ font-size: 1.1rem; font-weight: 900; }}
  .prod-cat  {{ font-size: 0.75rem; color: #555; }}
  .order-no  {{ font-size: 0.9rem; font-weight: 700; text-align: right; }}
  .patient   {{ font-size: 0.75rem; color: #555; text-align: right; }}

  table.cards {{ width: 100%; border-collapse: separate; border-spacing: 6px; }}
  .eye-card {{ width: 50%; vertical-align: top; }}
  .eye-card > div {{ border: 2px solid #000; border-radius: 6px; overflow: hidden; }}

  .eye-hdr  {{ padding: 5px 10px; font-size: 0.95rem; font-weight: 900;
               letter-spacing: .04em; border-bottom: 1px solid #ccc; }}
  .eye-hdr.right {{ background: #e8f5e9; color: #1b5e20; border-color: #2e7d32; }}
  .eye-hdr.left  {{ background: #e3f2fd; color: #0d47a1; border-color: #1565c0; }}
  .eye-card > div.right {{ border-color: #2e7d32; }}
  .eye-card > div.left  {{ border-color: #1565c0; }}

  .section {{ padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }}
  .section-label {{ font-size: 0.55rem; font-weight: 700; letter-spacing: .1em;
                    color: #777; margin-bottom: 4px; text-transform: uppercase; }}

  /* RX table */
  .rx-table {{ width: 100%; border-collapse: collapse; font-family: monospace; }}
  .rx-table th {{ font-size: 0.6rem; color: #777; font-weight: 600;
                  padding: 1px 4px; text-align: left; }}
  .rx-table td {{ font-size: 0.95rem; font-weight: 700; padding: 1px 4px; }}

  /* Surfacing powers */
  .surf-row {{ display: flex; gap: 6px; align-items: stretch; }}
  .surf-sph, .surf-base {{ flex: 1; text-align: center; padding: 4px; }}
  .surf-cyl {{ flex: 1.5; text-align: center; border: 2px solid #6d28d9;
               border-radius: 6px; padding: 5px 4px; background: #f5f3ff; }}
  .surf-label {{ font-size: 0.55rem; font-weight: 700; color: #777; }}
  .surf-val   {{ font-size: 1.2rem; font-weight: 900; font-family: monospace; }}
  .surf-cyl .surf-label {{ color: #6d28d9; }}
  .surf-cyl .surf-val   {{ font-size: 2rem; color: #4c1d95; }}
  .surf-axis  {{ font-size: 0.6rem; color: #7c3aed; margin-top: 2px; font-weight: 700; }}

  /* Tool + Diameter */
  .tool-row {{ display: flex; gap: 4px; }}
  .tool-box {{ flex: 1; border: 1px solid #1e40af; border-radius: 5px;
               padding: 5px; text-align: center; background: #eff6ff; }}
  .dia-box  {{ flex: 1; border: 1px solid #166534; border-radius: 5px;
               padding: 5px; text-align: center; background: #f0fdf4; }}
  .tool-label {{ font-size: 0.55rem; font-weight: 700; color: #1e40af; }}
  .dia-label  {{ font-size: 0.55rem; font-weight: 700; color: #166534; }}
  .tool-val {{ font-size: 1.3rem; font-weight: 900; font-family: monospace; color: #1e3a8a; }}
  .dia-val  {{ font-size: 1.3rem; font-weight: 900; font-family: monospace; color: #14532d; }}

  /* Blank / frame */
  .meta-row {{ display: flex; justify-content: space-between; font-size: 0.72rem; }}
  .meta-row b {{ font-weight: 700; color: #111; }}
  .notes {{ background: #fffbeb; border-left: 3px solid #f59e0b;
            padding: 3px 8px; margin-top: 5px; font-size: 0.7rem; color: #78350f; }}

  /* Sign-off */
  .signoff {{ border-top: 1px solid #e0e0e0; padding: 5px 10px;
              display: flex; justify-content: space-between;
              font-size: 0.65rem; color: #666; }}

  @media print {{
    body {{ padding: 5mm; }}
    .no-print {{ display: none !important; }}
  }}

  .print-btn {{
    margin: 10px 0 6px 0; padding: 8px 24px; background: #1e40af;
    color: #fff; border: none; border-radius: 6px; font-size: 0.9rem;
    font-weight: 700; cursor: pointer; }}
  .print-btn:hover {{ background: #1d4ed8; }}
</style>
</head>
<body>

<div class="no-print" style="margin-bottom:8px">
  <button class="print-btn" onclick="
    document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');
    window.print();
    setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),400);
  ">🖨️ Print Job Cards</button>
  <span style="font-size:0.75rem;color:#666;margin-left:10px">A4 Landscape recommended</span>
</div>

<!-- Header -->
<div class="header">
  <div>
    <div class="prod-name">{prod_name}</div>
    <div class="prod-cat">{category} &nbsp;·&nbsp; {brand}</div>
  </div>
  <div>
    <div class="order-no">Order: {order_no}</div>
    <div class="patient">Patient: {patient}</div>
    <div class="patient">{today}</div>
  </div>
</div>

<!-- R + L cards -->
<table class="cards">
<tr>

<!-- RIGHT EYE -->
<td class="eye-card">
  <div class="right">
    <div class="eye-hdr right">👁 RIGHT EYE &nbsp; <span style="font-size:0.65rem;font-weight:400;color:#555">ID: {R['line_id']}</span></div>

    <div class="section">
      <div class="section-label">Prescription (RX)</div>
      <table class="rx-table">
        <tr><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
        <tr>
          <td>{_sgn(R['sph_rx'])}</td>
          <td>{_sgn(R['cyl_rx'])}</td>
          <td>{R['axis_rx']}°</td>
          <td>{"+" + f"{R['add_rx']:.2f}" if R['add_rx'] else "—"}</td>
        </tr>
      </table>
    </div>

    <div class="section">
      <div class="section-label">Surfacing Powers</div>
      <div class="surf-row">
        <div class="surf-sph">
          <div class="surf-label">SPH</div>
          <div class="surf-val">{_sgn(R['sph_s']) if R['saved'] else "—"}</div>
        </div>
        <div class="surf-cyl">
          <div class="surf-label">CYL</div>
          <div class="surf-val">{_sgn(R['cyl_s']) if R['saved'] else "—"}</div>
          <div class="surf-axis">AXIS {R['axis_s'] if R['saved'] else "—"}°</div>
        </div>
        <div class="surf-base">
          <div class="surf-label">BASE</div>
          <div class="surf-val">{f"{R['base']:.2f}D" if R['saved'] else "—"}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-label">Tool &amp; Diameter</div>
      <div class="tool-row">
        <div class="tool-box">
          <div class="tool-label">TOOL A</div>
          <div class="tool-val">{R['tool_a'] if R['saved'] else "—"}</div>
        </div>
        <div class="tool-box">
          <div class="tool-label">TOOL B</div>
          <div class="tool-val">{R['tool_b'] if R['saved'] else "—"}</div>
        </div>
        <div class="dia-box">
          <div class="dia-label">DIAMETER</div>
          <div class="dia-val">{R['dia'] if R['saved'] else "—"}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="meta-row"><span>Blank: <b>{R['blank']}</b></span><span>Colour: <b>{R['colour']}</b></span></div>
      <div class="meta-row"><span>Frame: <b>{R['frame']}</b></span><span>Edge: <b>{R['edge']}</b></span></div>
      {"<div class='notes'>📝 " + R['notes'] + "</div>" if R['notes'] else ""}
    </div>
    <div class="signoff"><span>Tech: ______________</span><span>QC: _______________</span><span>Date: ____________</span></div>
  </div>
</td>

<td style="width:8px"></td>

<!-- LEFT EYE -->
<td class="eye-card">
  <div class="left">
    <div class="eye-hdr left">👁 LEFT EYE &nbsp; <span style="font-size:0.65rem;font-weight:400;color:#555">ID: {L['line_id']}</span></div>

    <div class="section">
      <div class="section-label">Prescription (RX)</div>
      <table class="rx-table">
        <tr><th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th></tr>
        <tr>
          <td>{_sgn(L['sph_rx'])}</td>
          <td>{_sgn(L['cyl_rx'])}</td>
          <td>{L['axis_rx']}°</td>
          <td>{"+" + f"{L['add_rx']:.2f}" if L['add_rx'] else "—"}</td>
        </tr>
      </table>
    </div>

    <div class="section">
      <div class="section-label">Surfacing Powers</div>
      <div class="surf-row">
        <div class="surf-sph">
          <div class="surf-label">SPH</div>
          <div class="surf-val">{_sgn(L['sph_s']) if L['saved'] else "—"}</div>
        </div>
        <div class="surf-cyl">
          <div class="surf-label">CYL</div>
          <div class="surf-val">{_sgn(L['cyl_s']) if L['saved'] else "—"}</div>
          <div class="surf-axis">AXIS {L['axis_s'] if L['saved'] else "—"}°</div>
        </div>
        <div class="surf-base">
          <div class="surf-label">BASE</div>
          <div class="surf-val">{f"{L['base']:.2f}D" if L['saved'] else "—"}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-label">Tool &amp; Diameter</div>
      <div class="tool-row">
        <div class="tool-box">
          <div class="tool-label">TOOL A</div>
          <div class="tool-val">{L['tool_a'] if L['saved'] else "—"}</div>
        </div>
        <div class="tool-box">
          <div class="tool-label">TOOL B</div>
          <div class="tool-val">{L['tool_b'] if L['saved'] else "—"}</div>
        </div>
        <div class="dia-box">
          <div class="dia-label">DIAMETER</div>
          <div class="dia-val">{L['dia'] if L['saved'] else "—"}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="meta-row"><span>Blank: <b>{L['blank']}</b></span><span>Colour: <b>{L['colour']}</b></span></div>
      <div class="meta-row"><span>Frame: <b>{L['frame']}</b></span><span>Edge: <b>{L['edge']}</b></span></div>
      {"<div class='notes'>📝 " + L['notes'] + "</div>" if L['notes'] else ""}
    </div>
    <div class="signoff"><span>Tech: ______________</span><span>QC: _______________</span><span>Date: ____________</span></div>
  </div>
</td>

</tr>
</table>
</body>
</html>"""

    # Render in iframe — self-contained, print button inside iframe prints only card
    st.components.v1.html(_print_html, height=620, scrolling=True)

    # Advance both to PRINTED on explicit button click
    _pb1, _pb2 = st.columns(2)
    with _pb1:
        if st.button("✅ Mark Both as PRINTED", key=f"mark_printed_{order_no}",
                     use_container_width=True):
            for _pl in (r_line, l_line):
                _jid = _get_job_master_id(_pl)
                if _jid:
                    try:
                        from modules.sql_adapter import run_scalar
                        run_scalar(
                            "SELECT public.advance_job_stage(%(j)s::uuid,'PRINTED',NULL::uuid)",
                            {"j": _jid})
                    except Exception: pass
            st.success("✅ Both job cards marked as PRINTED")
    with _pb2:
        if st.button("✕ Close", use_container_width=True,
                     key=f"close_pair_btn_{order_no}"):
            st.rerun()


def save_job_card_line(line: dict, order: dict) -> tuple[bool, str]:
    """
    Programmatic save of a job card line — same logic as the in-UI Save button.
    Called from backoffice_ui "Save Both Eyes" shared button.

    Returns (success: bool, message: str).
    Skips inventory deduction if job card already saved (idempotent re-save).
    """
    import json as _jsc

    surf = line.get("surfacing_data")
    if not surf:
        return False, f"No surfacing data for {line.get('eye_side','?')} eye — save the job card first"

    eye_side = (line.get("eye_side") or "").upper()

    # ── Determine if blank already deducted ───────────────────────────
    _already_saved = False
    try:
        from modules.sql_adapter import run_query as _rq
        _lid = (line.get("line_id") or line.get("id") or "").strip()
        if _lid:
            rows = _rq(
                "SELECT lens_params FROM order_lines WHERE id = %(l)s::uuid LIMIT 1",
                {"l": _lid}
            )
            if rows:
                _lp = rows[0].get("lens_params") or {}
                if isinstance(_lp, str):
                    try: _lp = _jsc.loads(_lp)
                    except: _lp = {}
                _already_saved = bool(_lp.get("surfacing_data"))
    except Exception:
        pass

    if _already_saved:
        # Re-save: update surfacing data only, no inventory touch
        _persist_surfacing_to_db(line)
        _upsert_job_master(line, order)
        return True, f"✅ {eye_side} eye job card updated"

    # ── First save: deduct blank inventory ────────────────────────────
    blank_id  = str(surf.get("blank_id") or "")
    if not blank_id:
        return False, f"No blank selected for {eye_side} eye"

    # Determine if blank is eye-specific
    try:
        from modules.sql_adapter import run_query as _rq2
        b_rows = _rq2(
            "SELECT is_eye_specific FROM blank_inventory WHERE id = %(b)s::uuid LIMIT 1",
            {"b": blank_id}
        )
        is_eye_specific = bool(b_rows[0].get("is_eye_specific")) if b_rows else True
    except Exception:
        is_eye_specific = True

    success = update_blank_quantity(
        blank_id=blank_id,
        qty_change=-1,
        eye_side=eye_side if is_eye_specific else None
    )

    if not success:
        return False, f"❌ Inventory update failed for {eye_side} eye — check blank availability"

    _persist_surfacing_to_db(line)
    _upsert_job_master(line, order)
    return True, f"✅ {eye_side} eye saved & inventory updated"

# ======================================================
# EXPORTS
# ======================================================

__all__ = [
    'render_surfacing_job_card',
    'render_job_card_print',
    'render_job_card_print_pair',
    'save_job_card_line',
]
