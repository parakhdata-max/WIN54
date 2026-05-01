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
    # STEP 4C: BASE CURVE PRE-FILTER
    # Show a base selector ONLY when multiple distinct bases exist
    # so the user picks the correct blank before seeing the list.
    # The chosen value pre-fills the Base Curve Selection dropdown later.
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

    _available_bases = []
    if not _pre_filter_blanks.empty:
        for _bc_col in ("base_recommended", "base_1", "base_2", "base_3"):
            if _bc_col in _pre_filter_blanks.columns:
                _vals = _pre_filter_blanks[_bc_col].dropna().astype(float)
                _available_bases.extend(_vals.tolist())
        _available_bases = sorted(set(round(b, 2) for b in _available_bases if b > 0))

    _base_pre_key = f"jc_base_pre_{_line_key(line)}"
    if len(_available_bases) > 1:
        _base_labels = [f"{b:.1f}D" for b in _available_bases]
        _prev_base_pre = st.session_state.get(_base_pre_key)
        _prev_idx = 0
        if _prev_base_pre and _prev_base_pre in _available_bases:
            _prev_idx = _available_bases.index(_prev_base_pre)
        _sel_base_label = st.selectbox(
            "Base Curve (pre-filter)",
            _base_labels,
            index=_prev_idx,
            key=f"jc_base_pre_sel_{_line_key(line)}",
            help="Select base to filter blanks — pre-fills Base Curve Selection below"
        )
        _pre_selected_base = _available_bases[_base_labels.index(_sel_base_label)]
        st.session_state[_base_pre_key] = _pre_selected_base
    elif _available_bases:
        _pre_selected_base = _available_bases[0]
        st.session_state[_base_pre_key] = _pre_selected_base
    else:
        _pre_selected_base = None

    # ==================================================
    # STEP 5: GET FILTERED BLANKS (base-filtered)
    # ==================================================
    if not _pre_filter_blanks.empty and _pre_selected_base is not None:
        # Filter to blanks that have this base in any base column
        _base_mask = pd.Series([False] * len(_pre_filter_blanks), index=_pre_filter_blanks.index)
        for _bc_col in ("base_recommended", "base_1", "base_2", "base_3"):
            if _bc_col in _pre_filter_blanks.columns:
                _base_mask |= (
                    _pre_filter_blanks[_bc_col].apply(
                        lambda v: abs(float(v) - _pre_selected_base) < 0.05 if pd.notna(v) else False
                    )
                )
        filtered_blanks = _pre_filter_blanks[_base_mask]
        if filtered_blanks.empty:
            filtered_blanks = _pre_filter_blanks  # fallback: show all if base filter returns nothing
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

    # If user pre-selected a base from the blank filter, use that as the recommended default
    _pre_base_from_filter = st.session_state.get(f"jc_base_pre_{_line_key(line)}")
    if _pre_base_from_filter:
        recommended_base = _pre_base_from_filter

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
                "base_curve_recommended": recommended_base,
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
    Single eye: shows that eye with blank R or L column.
    """
    import math as _m
    def _pf(v, d=0.0):
        try: f=float(v or d); return d if _m.isnan(f) or _m.isinf(f) else f
        except: return d
    def _pi(v): return int(_pf(v))
    def _sgn(v, blank=False):
        if blank: return ""
        f=_pf(v)
        return f"+{f:.2f}" if f>=0 else f"{f:.2f}"

    surf = line.get("surfacing_data") or {}
    if not surf:
        st.warning("⚠️ No surfacing data — save the job card first.")
        return

    eye = str(line.get("eye_side","")).upper()[:1]
    r_line = line if eye == "R" else None
    l_line = line if eye == "L" else None
    _open_jc_print_window(r_line, l_line, order)


def _open_jc_print_window(r_line, l_line, order: Dict):
    """Build job card HTML and open in new tab."""
    import math as _m
    import base64 as _b64
    import datetime as _dt
    import streamlit.components.v1 as _comp

    def _pf(v, d=0.0):
        try: f=float(v or d); return d if _m.isnan(f) or _m.isinf(f) else f
        except: return d
    def _pi(v): return int(_pf(v))
    def _sgn(v):
        f=_pf(v)
        return f"+{f:.2f}" if f>=0 else f"{f:.2f}"
    def _cell(v, blank=False):
        if blank: return ""
        return _sgn(v)

    order_no  = order.get("order_no","—")
    patient   = order.get("patient_name","—")
    today     = _dt.date.today().strftime("%d-%m-%Y")

    # Shop info
    shop_name = "DV Optical"
    shop_phone = ""
    try:
        from modules.settings.shop_master import get_unit_info
        _sh = get_unit_info("retail")
        shop_name  = _sh.get("shop_name", shop_name)
        shop_phone = _sh.get("shop_phone","")
    except Exception: pass

    def _eye_data(ln):
        if not ln: return None
        surf = ln.get("surfacing_data") or {}
        lp   = ln.get("lens_params") or {}
        if isinstance(lp, str):
            import json as _jlp
            try: lp = _jlp.loads(lp)
            except: lp = {}
        surf = surf or lp.get("surfacing_data") or {}
        return {
            "sph":    _pf(ln.get("sph")),
            "cyl":    _pf(ln.get("cyl")),
            "axis":   _pi(ln.get("axis")),
            "add":    _pf(ln.get("add_power")),
            "sph_s":  _pf(surf.get("sph_surf")),
            "cyl_s":  _pf(surf.get("cyl_surf")),
            "axis_s": _pi(surf.get("axis_surf")),
            "base":   _pf(surf.get("base_curve")),
            "tool_a": str(surf.get("tool_a") or ""),
            "tool_b": str(surf.get("tool_b") or ""),
            "blank":  f"{surf.get('blank_brand','')} {surf.get('blank_material','')}".strip(),
            "colour": surf.get("blank_colour") or "Clear",
            "dia":    str(surf.get("diameter") or ""),
            "frame":  str(surf.get("frame_type") or lp.get("frame_type") or ""),
            "notes":  str(surf.get("special_instructions") or ""),
            "batch":  str(surf.get("blank_batch") or ""),
        }

    R = _eye_data(r_line)
    L = _eye_data(l_line)

    product = (
        (r_line or l_line or {}).get("product_name","—")
    )

    # Phone number box (3403-style from image)
    phone_box = shop_phone or "—"

    # Barcode values
    bc_r_party = f"C{order_no.replace('-','').replace(' ','')}R"
    bc_r_order = f"O{order_no.replace('-','').replace(' ','')}R"
    bc_l_party = f"C{order_no.replace('-','').replace(' ','')}L"
    bc_l_order = f"O{order_no.replace('-','').replace(' ','')}L"

    def _barcode_img(val):
        try:
            from modules.printing.patient_card_printer import barcode_svg as _bsvg
            return _bsvg(val, width=140, height=35)
        except:
            return (
                f"<div style='border:1px solid #000;display:inline-block;padding:2px 4px'>"
                f"<div style='font-family:monospace;font-size:6pt;letter-spacing:2px'>{'|||' * (len(val)//2)}</div>"
                f"<div style='font-family:monospace;font-size:6pt'>{val}</div></div>"
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

    frame_val = (R or L or {}).get("frame","")
    dia_val   = (R or L or {}).get("dia","")
    blank_val = (R or L or {}).get("blank","")
    notes_val = (R or L or {}).get("notes","")

    _print_html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>
  @page {{ size: A5 landscape; margin: 8mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 8pt;
          background: #fff; color: #000; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ border: 1px solid #000; padding: 2px 4px; text-align: center;
        font-weight: 700; background: #f0f0f0; font-size: 7.5pt; }}
  td {{ border: 1px solid #000; padding: 2px 4px; font-size: 8pt; }}
  .hdr {{ text-align: center; border-bottom: 2px solid #000; padding-bottom: 3mm; margin-bottom: 3mm; }}
  .hdr .shop {{ font-size: 12pt; font-weight: 900; text-decoration: underline; }}
  .hdr .phone {{ font-size: 9pt; font-weight: 700; text-decoration: underline; }}
  .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2mm; margin-bottom: 3mm; }}
  .info-left .row, .info-right .row {{ font-size: 8pt; margin-bottom: 1mm; }}
  .info-left .row span, .info-right .row span {{ font-weight: 700; }}
  .section-label {{ font-size: 7.5pt; font-weight: 700; text-align: center;
                    border: 1px solid #000; background: #f0f0f0; padding: 1px 4px; }}
  .barcodes {{ display: flex; gap: 4mm; margin-top: 3mm; }}
  .bc-box {{ border: 1px solid #000; padding: 2mm; flex: 1; text-align: center; }}
  .no-print {{ display: block; }}
  @media print {{ .no-print {{ display: none !important; }} }}
  .print-btn {{ margin: 8px 0; padding: 8px 24px; background: #1e40af; color: #fff;
                border: none; border-radius: 6px; font-size: 12px; font-weight: 700;
                cursor: pointer; }}
</style>
</head><body>

<!-- HEADER -->
<div class="hdr">
  <div class="shop">{shop_name}</div>
  {f'<div class="phone">Phone No: {shop_phone}</div>' if shop_phone else ''}
</div>

<!-- INFO GRID -->
<div class="info-grid">
  <div class="info-left">
    <div class="row"><span>Phone No. : </span>{phone_box}</div>
    <div class="row"><span>Order No. : </span>{order_no}</div>
    <div class="row"><span>Date : </span>{today}</div>
    <div class="row"><span>Party Name : </span>{patient}</div>
    <div class="row"><span>Time : </span></div>
  </div>
  <div class="info-right">
    <div class="row"><span>Type : </span>{(R or L or {{}}).get('blank','').split()[-1] if (R or L) else ''}</div>
    <div class="row"><span>Coating : </span></div>
    <div class="row"><span>Material Of Lense : </span></div>
    <div class="row"><span>Material Of Index : </span></div>
    <div class="row"><span>Frame : </span>{frame_val}</div>
    <div class="row"><span>Diameter : </span>{dia_val}</div>
  </div>
</div>

<!-- POWER TABLE -->
<table style="margin-bottom:3mm">
  <tr>
    <th colspan="4">Right Power</th>
    <th colspan="4">Left Power</th>
  </tr>
  <tr>
    <th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
    <th>SPH</th><th>CYL</th><th>AXIS</th><th>ADD</th>
  </tr>
  {_row_rx(R, L)}
  {_row_surf(R, L)}
  <tr>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Base</td>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Total A</td>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Total B</td>
    <td style="border:1px solid #000;background:#f9f9f9"></td>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Base</td>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Total A</td>
    <td style="border:1px solid #000;text-align:center;font-size:7pt;background:#f0f0f0">Total B</td>
    <td style="border:1px solid #000;background:#f9f9f9"></td>
  </tr>
  {_row_tools(R, L)}
</table>

<!-- PARTICULARS TABLE -->
<table style="margin-bottom:3mm">
  <tr>
    <th style="width:8mm">Sr</th>
    <th>Particular</th>
    <th style="width:10mm">Qty</th>
  </tr>
  <tr>
    <td style="text-align:center">1</td>
    <td>{product}{f' — {blank_val}' if blank_val else ''}</td>
    <td style="text-align:center">1</td>
  </tr>
</table>

<!-- CLIENT DETAILS + BARCODES -->
<div style="font-size:8pt;margin-bottom:1mm"><strong>Client Details :</strong></div>
<div class="barcodes">
  {'<div class="bc-box">' + _barcode_img(bc_r_party) + '<br>' + _barcode_img(bc_r_order) + '</div>' if r_line else ''}
  {'<div class="bc-box">' + _barcode_img(bc_l_party) + '<br>' + _barcode_img(bc_l_order) + '</div>' if l_line else ''}
</div>

{f'<div style="margin-top:2mm;font-size:7.5pt;color:#555">Notes: {notes_val}</div>' if notes_val else ''}

<!-- PRINT BUTTON -->
<div class="no-print" style="text-align:center;padding:12px">
  <button class="print-btn" onclick="
    document.querySelectorAll('.no-print').forEach(e=>e.style.display='none');
    window.print();
    setTimeout(()=>document.querySelectorAll('.no-print').forEach(e=>e.style.display=''),600);
  ">🖨️ Print Job Card</button>
</div>

</body></html>"""

    _b64_html = _b64.b64encode(_print_html.encode("utf-8")).decode()
    _comp.html(
        f"<script>(function(){{"
        f"var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});"
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
                except: _lp = {}
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
            _is_eye_specific = any(x in _bcat for x in ("PROGRESSIVE", "D BIFOCAL"))
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
        except: return "—"

    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:75mm 55mm;margin:0}}body{{margin:0;font-family:Arial}}
    .lbl{{width:75mm;height:55mm;box-sizing:border-box;padding:3mm 4mm;background:#fff;border:.5mm solid #333}}
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
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
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
        except: return "—"

    _html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page{{size:85mm 54mm;margin:0}}body{{margin:0;font-family:Arial}}
    .card{{width:85mm;height:54mm;box-sizing:border-box;padding:4mm 5mm;
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
        f"<script>(function(){{var b=new Blob([atob('{_b64_html}')],{{type:'text/html'}});window.open(URL.createObjectURL(b),'_blank')}})();</script>",
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
