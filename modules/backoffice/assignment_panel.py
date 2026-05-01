"""
assignment_panel.py
===================
Supplier / Job-Card Assignment Panel — sits at the bottom of the
backoffice order detail view, ABOVE the Save button.

WHAT IT DOES
------------
  • Shows every unconfirmed line with a route choice:
      Supplier  |  Job Card (In-house)  |  External Lab
  • For Ophthalmic + Contact lens lines marked lens_item_type='RX':
      Always Supplier — no route radio, just supplier dropdown
  • For stock-allocated lines:
      Shows current allocation summary — no re-routing needed
  • Confirm All Assignments button locks the routes before save
  • SHIFT button — lets operator move a line between Supplier ↔ Job
    WITHOUT going into the save window — live, instant, no re-save needed

STATE KEYS
----------
  bo_assignments          : dict  { line_key → {route, supplier_id, supplier_name, job_type, confirmed} }
  bo_assignments_locked   : bool  — True after Confirm All clicked
  bo_shift_target         : str   — line_key being shifted (for shift modal)

ROUTING VALUES
--------------
  STOCK         — already allocated from inventory (no action needed)
  VENDOR        — goes to external supplier
  INHOUSE       — job card for in-house lab
  EXTERNAL_LAB  — job card + external lab supplier

DB IMPACT
---------
  Changes manufacturing_route on the line dict (in-memory).
  categorize_order_lines(order) is called after any change
  so stock_lines / inhouse_lines / lab_order_lines stay in sync.
  No DB write happens here — save_order_to_db() does that on Save.
"""

import streamlit as st
from typing import Dict, List, Optional

from .backoffice_helpers import (
    get_display_order_id,
    categorize_order_lines,
)
try:
    from modules.core.eye_side_normalizer import normalize_eye_side
except ImportError:
    def normalize_eye_side(v, **_):  # noqa: F811
        if not v:
            return "B"
        _k = str(v).strip().upper()
        if _k in ("R", "RIGHT", "RE"):   return "R"
        if _k in ("L", "LEFT", "LE"):    return "L"
        if _k in ("S", "SVC", "SERVICE", "SERVICES"): return "SERVICE"
        return "B"


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

ROUTE_VENDOR       = "VENDOR"
ROUTE_INHOUSE      = "INHOUSE"
ROUTE_EXTERNAL_LAB = "EXTERNAL_LAB"
ROUTE_STOCK        = "STOCK"

ROUTE_LABELS = {
    ROUTE_VENDOR:       "🏭 Supplier (Direct)",
    ROUTE_INHOUSE:      "🔬 In-house Lab",
    ROUTE_EXTERNAL_LAB: "🧪 External Lab",
    ROUTE_STOCK:        "📦 From Stock",
}

# Routes the operator can pick — includes STOCK so any eye can be assigned from stock
ASSIGNABLE_ROUTES = [ROUTE_VENDOR, ROUTE_INHOUSE, ROUTE_EXTERNAL_LAB, ROUTE_STOCK]


# ═══════════════════════════════════════════════════════════════════════
# SESSION STATE INIT
# ═══════════════════════════════════════════════════════════════════════

def init_assignment_state(all_lines=None):
    if "bo_assignments" not in st.session_state:
        st.session_state.bo_assignments = {}
    if "bo_shift_target" not in st.session_state:
        st.session_state.bo_shift_target = None
    if "bo_assignments_locked" not in st.session_state:
        st.session_state.bo_assignments_locked = False

    if all_lines:
        _has_job_card = _any_line_has_job_card(all_lines)
        if _has_job_card:
            st.session_state.bo_assignments_locked = True

        # ── Auto-populate assignments from existing line data ─────────────
        # Stock-allocated and vendor-assigned lines already have their route
        # saved in the DB. Pre-fill bo_assignments so the panel shows them as
        # confirmed without requiring a manual "Confirm All" click.
        assignments = st.session_state.bo_assignments
        _any_auto = False
        for idx, line in enumerate(all_lines):
            lk = _line_key(line, idx)
            if lk in assignments:
                continue  # already set this session — don't overwrite

            route  = str(line.get("manufacturing_route") or "").upper()
            alloc  = int(line.get("allocated_qty") or 0)
            needed = int(line.get("billing_qty") or line.get("quantity") or 1)
            supp   = str(line.get("supplier_id") or "")

            # batch_no = SKU was picked from inventory at punching time.
            # This is the primary STOCK signal for frames/accessories.
            # manufacturing_route is decoded from lens_params JSON by order_loader
            # and takes precedence when explicitly set.
            _lp = line.get("lens_params") or {}
            _lp = _lp if isinstance(_lp, dict) else {}
            _batch_no = str(line.get("batch_no") or _lp.get("batch_no") or "").strip()

            # Resolve route: explicit saved route wins; batch_no is next;
            # allocated_qty is last resort (lenses only, never reliable for frames).
            if not route:
                if _batch_no:
                    route = "STOCK"   # batch_no set → item was picked from inventory
                elif alloc >= needed and needed > 0:
                    route = "STOCK"   # lens allocation recorded in DB

            if route == "STOCK":
                assignments[lk] = {
                    "route":     ROUTE_STOCK,
                    "job_type":  "STOCK",
                    "confirmed": True,
                }
                _any_auto = True
            elif route == "VENDOR" or (not route and supp):
                assignments[lk] = {
                    "route":       ROUTE_VENDOR,
                    "supplier_id": supp,
                    "supplier_name": str(line.get("supplier_name") or ""),
                    "confirmed":   bool(supp),
                }
                _any_auto = True
            elif route == "INHOUSE":
                assignments[lk] = {"route": ROUTE_INHOUSE, "confirmed": True}
                _any_auto = True
            elif route == "EXTERNAL_LAB":
                assignments[lk] = {"route": ROUTE_EXTERNAL_LAB, "confirmed": True}
                _any_auto = True

        # If all lines are now auto-confirmed, lock the panel silently
        if _any_auto and all_lines:
            _all_confirmed = all(
                assignments.get(_line_key(l, i), {}).get("confirmed")
                for i, l in enumerate(all_lines)
            )
            if _all_confirmed and not _has_job_card:
                st.session_state.bo_assignments_locked = True


def _safe_unlock(all_lines) -> bool:
    """
    Unlock assignments only if no job card has been saved.
    Returns True if unlock succeeded, False if blocked.
    """
    if _any_line_has_job_card(all_lines):
        return False
    st.session_state.bo_assignments_locked = False
    return True


def _any_line_has_job_card(all_lines) -> bool:
    """True if any line has surfacing_data persisted (blank allocated, job card saved)."""
    import json as _jap
    from typing import List
    for line in all_lines:
        # Fast path: unpacked by order_loader
        if line.get("surfacing_data"):
            return True
        # Fallback: check lens_params blob
        lp = line.get("lens_params") or {}
        if isinstance(lp, str):
            try: lp = _jap.loads(lp)
            except: lp = {}
        if lp.get("surfacing_data"):
            return True
    # DB check — catches cases where order_loader is old version
    try:
        import json as _j2
        from modules.sql_adapter import run_query as _rqap
        line_ids = [
            (line.get("line_id") or line.get("id") or "").strip()
            for line in all_lines
            if (line.get("line_id") or line.get("id") or "").strip()
        ]
        if line_ids:
            rows = _rqap("""
                SELECT id FROM order_lines
                WHERE id = ANY(%(ids)s::uuid[])
                  AND lens_params::jsonb ? 'surfacing_data'
                LIMIT 1
            """, {"ids": line_ids})
            if rows:
                return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _line_key(line: Dict, idx: int) -> str:
    """Stable key for a line — product + eye + index."""
    return f"{line.get('product_id', 'unk')}_{line.get('eye_side', 'B')}_{idx}"


def _is_rx(line: Dict) -> bool:
    return str(line.get("lens_item_type", "")).upper() == "RX"


def _is_stock_allocated(line: Dict) -> bool:
    """
    True when this line is confirmed as fulfilled from stock.

    Signal priority (in order):
      1. manufacturing_route == 'STOCK'  → always STOCK (explicit, saved to DB)
      2. batch_no set on line or in lens_params → STOCK (frame/SKU picked from inventory)
      3. allocated_qty >= billing_qty → STOCK (lens allocation recorded in DB)

    IMPORTANT: For frames, allocated_qty is NEVER reliable — it starts at 0 on
    new orders and is only updated on explicit allocation save. batch_no is set
    at punching time and is the correct signal for frames.
    """
    route  = str(line.get("manufacturing_route") or "").upper()
    if route == ROUTE_STOCK:
        return True

    # batch_no present → SKU was picked from inventory at punching time
    _lp       = line.get("lens_params") or {}
    _lp       = _lp if isinstance(_lp, dict) else {}
    _batch_no = str(line.get("batch_no") or _lp.get("batch_no") or "").strip()
    if _batch_no:
        return True

    # Fallback for lenses: allocation recorded in DB
    alloc  = int(line.get("allocated_qty") or 0)
    needed = int(line.get("billing_qty") or line.get("quantity") or 1)
    return alloc >= needed and needed > 0


def _is_frame_line(line: Dict) -> bool:
    """
    True when the line is a frame or sunglass.
    Frames are SKU-based — colour/SKU can be changed after stock allocation.
    Uses main_group (always stored on the line) as the primary signal,
    with a fallback to batch_no pattern set by retail/wholesale punching.
    """
    main_group = (line.get("main_group") or "").lower()
    if "frame" in main_group or "sunglass" in main_group:
        return True
    # Fallback: frame punched with a batch_no (SKU) and eye_side normalizes to B
    # (retail used to save 'OTHER', wholesale too — now both normalize to B via loader)
    eye = normalize_eye_side(line.get("eye_side"))
    if eye == "B" and line.get("batch_no") and not line.get("sph"):
        return True
    return False


def _requires_batch_expiry(line: Dict) -> bool:
    """
    True for products that carry an expiry date and MUST have a batch selected
    at assignment time — not just any stock allocation.

    Rules:
      • main_group contains 'solution'  → contact lens solutions (e.g. Opti-Free, ReNu)
      • main_group contains 'cleaner'   → lens cleaners / enzyme tablets
      • main_group contains 'drop'      → eye drops
      • main_group contains 'medicine'  → any dispensed medicine

    Contact lenses themselves are handled separately (ALREADY PERFECT — no change).
    Frames / accessories / ophthalmic lenses do NOT have expiry dates.
    """
    mg = (line.get("main_group") or "").lower()
    return any(g in mg for g in ("solution", "cleaner", "drop", "medicine"))


def _eye_badge(line: Dict) -> str:
    eye = str(line.get("eye_side", "")).upper()
    if eye in ("R", "RIGHT"):
        return "👁 RE"
    if eye in ("L", "LEFT"):
        return "👁 LE"
    return "👁 B"


def _power_str(line: Dict) -> str:
    import math
    def _safe(v):
        """Return float or None — converts nan/None/empty to None."""
        if v is None: return None
        try:
            f = float(v)
            return None if math.isnan(f) or math.isinf(f) else f
        except (TypeError, ValueError):
            return None

    parts = []
    sph  = _safe(line.get("sph"))
    cyl  = _safe(line.get("cyl"))
    axis = _safe(line.get("axis"))
    add  = _safe(line.get("add_power"))

    if sph is not None:
        parts.append(f"SPH {sph:+.2f}")
    if cyl is not None and abs(cyl) > 0.01:
        parts.append(f"CYL {cyl:+.2f}")
        if axis is not None:
            parts.append(f"AX {int(axis)}°")
    if add is not None and abs(add) > 0.01:
        parts.append(f"ADD {add:+.2f}")
    return "  ".join(parts) if parts else ""


def _get_suppliers() -> List[Dict]:
    """
    Fetch active suppliers from parties table.
    Returns list of {id, name}.
    Falls back to empty list if DB unavailable.
    """
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT id::text AS id, party_name AS name,
                   mobile, gstin, credit_days, credit_limit
            FROM parties
            WHERE LOWER(COALESCE(party_type,'')) IN ('supplier','vendor')
              AND COALESCE(is_active, true) = true
            ORDER BY party_name
        """, {})
        return rows if rows else []
    except Exception:
        return []


def _get_ranked_suppliers_for_product(
    product_id,
    route_type: str = "VENDOR"
) -> List[Dict]:
    """
    Priority:
      1. product_supplier_map — admin-defined ranked list (primary + alternates)
      2. Supplier order history — ranked by past usage
      3. All active suppliers — full fallback

    Returns list of {id, name, past_orders, is_primary, notes}
    """
    if not product_id:
        return _get_suppliers()

    # Priority 1: product_supplier_map (direct DB query — no helper dependency)
    try:
        from modules.sql_adapter import run_query as _rq_psm
        mapped_rows = _rq_psm("""
            SELECT
                psm.supplier_id::text  AS supplier_id,
                p.party_name           AS supplier_name,
                psm.rank,
                psm.notes,
                (psm.rank = 1)         AS is_primary
            FROM product_supplier_map psm
            JOIN parties p ON p.id = psm.supplier_id
            WHERE psm.product_id = %(pid)s::uuid
              AND psm.route_type  = %(rt)s
              AND psm.is_active   = TRUE
            ORDER BY psm.rank
        """, {"pid": str(product_id), "rt": route_type}) or []

        if mapped_rows:
            mapped_ids = {r["supplier_id"] for r in mapped_rows}
            all_sups   = _get_suppliers()
            extras     = [s for s in all_sups if s["id"] not in mapped_ids]
            return [
                {
                    "id":         r["supplier_id"],
                    "name":       r["supplier_name"],
                    "past_orders": 0,
                    "is_primary": bool(r["is_primary"]),
                    "notes":      r.get("notes") or "",
                    "rank":       int(r["rank"]),
                }
                for r in mapped_rows
            ] + [
                {"id": s["id"], "name": s.get("name",""), "past_orders": 0,
                 "is_primary": False, "notes": "", "rank": 99}
                for s in extras
            ]
    except Exception:
        pass

    # Priority 2: order history
    try:
        from modules.sql_adapter import run_query
        rows = run_query("""
            SELECT
                p.id::text           AS id,
                p.party_name         AS name,
                COUNT(soi.id)        AS past_orders
            FROM parties p
            LEFT JOIN supplier_orders so  ON so.supplier_id::uuid = p.id
            LEFT JOIN supplier_order_items soi
                   ON soi.supplier_order_id = so.id
                  AND soi.product_id::text = %(pid)s
            WHERE LOWER(COALESCE(p.party_type,'')) IN ('supplier','vendor')
              AND COALESCE(p.is_active, true) = true
            GROUP BY p.id, p.party_name
            ORDER BY past_orders DESC, p.party_name ASC
        """, {"pid": str(product_id)})
        if rows:
            return [dict(r, is_primary=False, notes="") for r in rows]
    except Exception:
        pass

    # Priority 3: all suppliers
    return [dict(s, is_primary=False, notes="") for s in _get_suppliers()]


# ═══════════════════════════════════════════════════════════════════════
# MAIN RENDER
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# PRODUCT TYPE CLASSIFIERS
# ═══════════════════════════════════════════════════════════════════════

def _is_ophthalmic_lens(line: Dict) -> bool:
    """
    True for ophthalmic spectacle lenses — need a Job Card (in-house or external lab).

    Detection priority:
      1. main_group explicitly contains 'ophthalmic' → True
      2. main_group explicitly contains 'contact', 'frame', 'solution',
         'accessory', 'accessories', 'spare', 'case', 'cloth' → False
         (these are stock-only; contact lenses go to supplier/RX path)
      3. eye_side R/L with a valid SPH value → True (prescription lens)
      4. Everything else → False
    """
    main_group = (line.get("main_group") or "").lower()

    # Explicit ophthalmic match
    if "ophthalmic" in main_group:
        return True

    # Explicit non-ophthalmic groups — never job card
    non_ophthalmic = ("contact", "frame", "solution", "cleaner", "accessory",
                      "accessories", "spare", "case", "cloth", "tool")
    if any(g in main_group for g in non_ophthalmic):
        return False

    # Eye_side R/L with a real SPH value → prescription spectacle lens
    eye = str(line.get("eye_side", "")).upper()
    if eye in ("R", "L", "RIGHT", "LEFT"):
        sph = line.get("sph")
        if sph is not None:
            try:
                import math
                f = float(sph)
                if not (math.isnan(f) or math.isinf(f)):
                    return True
            except (TypeError, ValueError):
                pass

    return False


def _is_stock_only_product(line: Dict) -> bool:
    """
    True for products that come from stock — frames, solutions, accessories,
    contact lenses, and any eye_side OTHER than R/L/SERVICE.
    These show only Supplier + Stock routes (no Job Card options).
    """
    main_group = (line.get("main_group") or "").lower()

    # Explicit stock-only groups — these use SKU/frame stock path (no expiry)
    # NOTE: 'solution' and 'cleaner' are intentionally excluded — they carry
    # expiry dates and are routed through _render_expiry_batch_selector instead.
    stock_groups = ("frame", "accessory", "accessories",
                    "spare", "case", "cloth", "tool", "contact")
    if any(g in main_group for g in stock_groups):
        return True

    # eye_side OTHER / B / blank / anything not R L SERVICE → stock item
    eye = str(line.get("eye_side", "")).upper()
    if eye not in ("R", "L", "RIGHT", "LEFT", "SERVICE", "S"):
        return True

    # eye_side R/L but NO sph set and NOT ophthalmic main_group
    # → likely a frame punched on eye_side R (e.g. Butler 8308)
    if eye in ("R", "L", "RIGHT", "LEFT"):
        sph = line.get("sph")
        has_power = False
        if sph is not None:
            try:
                import math
                f = float(sph)
                has_power = not (math.isnan(f) or math.isinf(f))
            except (TypeError, ValueError):
                pass
        if not has_power and "ophthalmic" not in main_group:
            return True

    return False


def _get_job_card_routes_for_line(line: Dict):
    """
    Return (available_routes, default_route) based on product type.

    Ophthalmic lens  → [Supplier, In-house, External Lab]   default=In-house
    Stock-only item  → [Supplier, Stock]                     default=Stock (or Supplier)
    Contact lens/RX  → handled separately by _is_rx()
    Everything else  → all 4 routes                          default=Supplier
    """
    if _is_ophthalmic_lens(line):
        # Ophthalmic lens routes — all four are valid:
        # Stock        = pre-made lens available in inventory (allocated)
        # In-house Lab = we surface/process in-house
        # External Lab = send to external lab for processing
        # Supplier     = direct from supplier (pre-made, ordered in)
        routes = [ROUTE_STOCK, ROUTE_INHOUSE, ROUTE_EXTERNAL_LAB, ROUTE_VENDOR]
        # Smart default: if batch_no set or already allocated from stock → STOCK
        _lp_oph     = line.get("lens_params") or {}
        _lp_oph     = _lp_oph if isinstance(_lp_oph, dict) else {}
        _bn_oph     = str(line.get("batch_no") or _lp_oph.get("batch_no") or "").strip()
        _alloc_oph  = int(line.get("allocated_qty") or 0)
        _needed_oph = int(line.get("billing_qty") or line.get("quantity") or 1)
        _bs_oph     = str(line.get("batch_status") or "").upper()
        if _bn_oph or _alloc_oph >= _needed_oph or _bs_oph == "ALLOCATED":
            default = ROUTE_STOCK
        else:
            # Fall back to whatever is already saved, else INHOUSE
            _saved_route = str(line.get("manufacturing_route") or "").upper()
            default = _saved_route if _saved_route in routes else ROUTE_INHOUSE
        return routes, default

    if _is_stock_only_product(line):
        # Frame/accessory: STOCK if batch_no is set (SKU picked at punching),
        # or if allocated_qty is filled (legacy lens-style allocation).
        # Never INHOUSE — no lab processing for frames/accessories.
        _lp       = line.get("lens_params") or {}
        _lp       = _lp if isinstance(_lp, dict) else {}
        _batch_no = str(line.get("batch_no") or _lp.get("batch_no") or "").strip()
        _alloc    = int(line.get("allocated_qty") or 0)
        _needed   = int(line.get("billing_qty") or line.get("quantity") or 1)
        _default  = ROUTE_STOCK if (_batch_no or _alloc >= _needed) else ROUTE_VENDOR
        return [ROUTE_STOCK, ROUTE_VENDOR], _default

    if _requires_batch_expiry(line):
        # Solutions / cleaners / drops: STOCK (FEFO batch) or VENDOR
        # Never INHOUSE — these are dispensed, not processed
        _lp       = line.get("lens_params") or {}
        _lp       = _lp if isinstance(_lp, dict) else {}
        _batch_no = str(line.get("batch_no") or _lp.get("batch_no") or "").strip()
        _alloc    = int(line.get("allocated_qty") or 0)
        _needed   = int(line.get("billing_qty") or line.get("quantity") or 1)
        _default  = ROUTE_STOCK if (_batch_no or _alloc >= _needed) else ROUTE_VENDOR
        return [ROUTE_STOCK, ROUTE_VENDOR], _default

    # Fallback — show all routes
    return list(ASSIGNABLE_ROUTES), ROUTE_VENDOR


def render_assignment_panel(order: Dict, all_lines: List[Dict]) -> None:
    """
    Main entry point.  Call this in backoffice_ui.py just above the
    Billing Gate save button.

    Layout:
      ┌─────────────────────────────────────────────────────────┐
      │  🎯  SUPPLIER / JOB ASSIGNMENT                          │
      │  ── one row per line ──                                  │
      │  [Confirm All Assignments]   [Unlock & Edit]             │
      └─────────────────────────────────────────────────────────┘
    """
    from modules.core.business_rules import SERVICE_EYE_SIDES

    # SERVICE lines (consultation fee, eye testing, misc charges) are
    # auto-allocated and never need supplier/job assignment — filter them out.
    _service_lines = [
        l for l in all_lines
        if str(l.get("eye_side","")).upper() in SERVICE_EYE_SIDES
        or bool(l.get("is_service_line"))
    ]
    all_lines = [
        l for l in all_lines
        if str(l.get("eye_side","")).upper() not in SERVICE_EYE_SIDES
        and not l.get("is_service_line")
    ]

    # If only service lines exist, show a note and return
    if not all_lines:
        if _service_lines:
            st.info(
                f"🩺 {len(_service_lines)} service line(s) — "
                "consultation / eye-testing fees are auto-allocated, no assignment needed."
            )
        return

    # Show service lines as a read-only note
    if _service_lines:
        _svc_names = ", ".join(
            str(l.get("product_name") or "Service") for l in _service_lines
        )
        st.markdown(
            f"<div style='background:#0f1e0f;border:1px solid #22c55e33;"
            f"border-radius:6px;padding:6px 12px;margin-bottom:6px;"
            f"color:#86efac;font-size:0.72rem'>"
            f"🩺 Auto-allocated service lines (no assignment needed): {_svc_names}"
            f"</div>",
            unsafe_allow_html=True,
        )

    init_assignment_state(all_lines)
    oid = get_display_order_id(order)

    st.markdown("---")

    # ── Smart header with corner Assign button ────────────────────────────
    _hc1, _hc2 = st.columns([3, 1])
    with _hc1:
        st.markdown("### 🎯 Supplier / Job Assignment")
        # Count how many lines still need manual assignment
        _assignments_now = st.session_state.get("bo_assignments", {})
        _need_assign = sum(
            1 for i, l in enumerate(all_lines)
            if not _assignments_now.get(_line_key(l, i), {}).get("confirmed")
            and not _is_stock_allocated(l)
        )
        _total_lines = len(all_lines)
        _done_count  = _total_lines - _need_assign
        if _need_assign == 0:
            st.caption(f"✅ All {_total_lines} line(s) assigned — ready to save")
        else:
            st.caption(
                f"📋 {_done_count}/{_total_lines} assigned · "
                f"{_need_assign} still need route selection"
            )
    with _hc2:
        _locked_now = st.session_state.get("bo_assignments_locked", False)
        if not _locked_now:
            if st.button(
                "✅ Assign All",
                key=f"corner_assign_{oid}",
                type="primary",
                use_container_width=True,
                help="Confirm all assignments and lock for saving"
            ):
                _apply_all_assignments(order, all_lines)
                st.session_state.bo_assignments_locked = True
                st.rerun()
        else:
            st.markdown(
                "<div style='text-align:center;padding:6px 4px;"
                "background:#0f2a1a;border:1px solid #22c55e33;"
                "border-radius:6px;color:#86efac;font-size:0.78rem;"
                "font-weight:700'>✅ Assigned</div>",
                unsafe_allow_html=True
            )

    # ── Reassignment warning ────────────────────────────────────────────────
    # Triggered when:
    #   - Order has been saved before (status is past PENDING), AND
    #   - Lines already have allocation / route / supplier set (from DB), AND
    #   - User has not yet acknowledged the warning THIS SESSION for THIS order
    #
    # Key design: warning reappears every new session (page reload) — intentional.
    # Once acknowledged in a session it won't re-block within the same session.
    _order_status = order.get("status", "PENDING")
    _already_saved = _order_status not in ("PENDING", "PENDING_VALIDATION", "PROVISIONAL", "ORDER_SAVED", "")
    _has_existing_assignments = any(
        l.get("manufacturing_route") or l.get("supplier_id") or int(l.get("allocated_qty") or 0) > 0
        for l in all_lines
    )

    # Session key scoped to order — reset when user navigates away and comes back
    _warn_key      = f"reassign_warned_{oid}"
    _warn_ack_key  = f"reassign_ack_{oid}"

    # If panel is being opened fresh this session, reset ack so warning shows
    if _warn_key not in st.session_state:
        st.session_state[_warn_key]     = True   # warning is active
        st.session_state[_warn_ack_key] = False  # not yet acknowledged

    if _already_saved and _has_existing_assignments and not st.session_state[_warn_ack_key]:
        st.warning(
            "⚠️ **This order was previously saved with assignments.** "
            "Modifying assignments may affect supplier orders and stock allocation. "
            "Only proceed if you intend to reassign."
        )
        col_w1, col_w2 = st.columns([1, 1])
        with col_w1:
            if st.button("✅ Yes, reassign this order", key=f"reassign_yes_{oid}", type="primary"):
                st.session_state[_warn_ack_key] = True
                if not _safe_unlock(all_lines):
                    st.error(
                        "🔒 Cannot reassign — job card has been saved for one or more lines. "
                        "Go to **Documents → Job Cards** and cancel the job card first."
                    )
                st.rerun()
        with col_w2:
            if st.button("❌ Cancel — keep existing", key=f"reassign_no_{oid}"):
                st.session_state.bo_assignments_locked = True
                st.rerun()
        # Return (not st.stop) — tabs outside this panel must keep rendering
        return

    locked = st.session_state.bo_assignments_locked

    if locked:
        _render_locked_summary(order, all_lines, oid)
    else:
        _render_assignment_rows(order, all_lines, oid)

    # ── Confirm / Unlock buttons ────────────────────────────────────
    st.markdown("")
    col_confirm, col_unlock, col_status = st.columns([2, 1, 3])

    with col_confirm:
        if not locked:
            if st.button(
                "✅ Confirm All Assignments",
                type="primary",
                use_container_width=True,
                key=f"confirm_assignments_{oid}",
            ):
                _apply_all_assignments(order, all_lines)
                st.session_state.bo_assignments_locked = True
                st.rerun()
        else:
            # Check if order is already confirmed
            current_status = order.get("status", "")
            if current_status == "CONFIRMED":
                # Get confirmation timestamp from order
                confirmed_at = order.get("confirmed_at") or order.get("updated_at") or order.get("created_at")
                if confirmed_at:
                    try:
                        # Format timestamp nicely
                        if isinstance(confirmed_at, str):
                            from datetime import datetime
                            # Try to parse the timestamp
                            if "T" in confirmed_at:  # ISO format
                                dt = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
                            else:
                                dt = datetime.strptime(confirmed_at[:19], "%Y-%m-%d %H:%M:%S")
                            formatted_time = dt.strftime("%d %b %Y at %I:%M %p")
                            st.success(f"✅ Order already confirmed on {formatted_time}")
                        else:
                            st.success("✅ Order already confirmed")
                    except Exception:
                        st.success("✅ Order already confirmed")
                else:
                    st.success("✅ Order already confirmed")
            else:
                st.success("✅ Assignments confirmed — ready to save")

    with col_unlock:
        if locked:
            _job_card_locked = _any_line_has_job_card(all_lines)
            if _job_card_locked:
                st.markdown(
                    "<div style='background:#1a0a00;border:1px solid #f97316;"
                    "border-radius:6px;padding:6px 12px;text-align:center'>"
                    "<span style='color:#fb923c;font-size:0.8rem;font-weight:700'>"
                    "🔒 Job card saved<br>"
                    "<span style='font-size:0.72rem;font-weight:400'>"
                    "Cancel job card first</span></span></div>",
                    unsafe_allow_html=True
                )
            else:
                if st.button(
                    "✏️ Edit",
                    use_container_width=True,
                    key=f"unlock_assignments_{oid}",
                ):
                    if not _safe_unlock(all_lines):
                        st.error("🔒 Cancel job card first (Documents → Job Cards)")
                    st.rerun()

    with col_status:
        _render_assignment_summary_chips(all_lines, oid)


# ═══════════════════════════════════════════════════════════════════════
# ROW RENDERER (unlocked / editing state)
# ═══════════════════════════════════════════════════════════════════════

def _render_assignment_rows(order: Dict, all_lines: List[Dict], oid: str):
    """Render assignment rows — R and L side by side grouped by product."""

    assignments = st.session_state.bo_assignments

    # ── Group lines R/L by product ───────────────────────────────────
    # SERVICE lines are filtered out at render_assignment_panel entry — safe to iterate
    from modules.core.business_rules import SERVICE_EYE_SIDES
    groups: dict = {}
    for idx, line in enumerate(all_lines):
        if str(line.get("eye_side","")).upper() in SERVICE_EYE_SIDES or line.get("is_service_line"):
            continue   # belt-and-suspenders skip
        pid = line.get("product_id") or line.get("product_name", f"line_{idx}")
        if pid not in groups:
            groups[pid] = {
                "product_name": line.get("product_name", "Unknown"),
                "R": None, "R_idx": None,
                "L": None, "L_idx": None,
            }
        eye = str(line.get("eye_side", "")).upper().strip()
        if eye in ("R", "RIGHT") and groups[pid]["R"] is None:
            groups[pid]["R"] = line
            groups[pid]["R_idx"] = idx
        elif eye in ("L", "LEFT") and groups[pid]["L"] is None:
            groups[pid]["L"] = line
            groups[pid]["L_idx"] = idx
        else:
            # Solo / unmatched — own group
            solo_key = f"{pid}_{idx}"
            groups[solo_key] = {
                "product_name": line.get("product_name", "Unknown"),
                "R": line, "R_idx": idx,
                "L": None,  "L_idx": None,
            }

    # ── Render each group ────────────────────────────────────────────
    for grp in groups.values():
        has_r = grp["R"] is not None
        has_l = grp["L"] is not None

        # ── Compact header for frame/other lines ───────────────
        # Frame lines: show single-line chip — edit handled in frame card above
        # Lens R/L lines: show full assignment card
        _grp_is_frame = all(
            _is_frame_line(l) or str((l or {}).get("eye_side","")).upper()
            not in ("R","RIGHT","L","LEFT")
            for l in [grp["R"], grp["L"]] if l is not None
        )

        if _grp_is_frame:
            # Frame / Other items — compact chip + route-change expander
            _fl = grp["R"] or grp["L"]
            _fl_idx = grp["R_idx"] if grp["R"] else grp["L_idx"]
            if _fl:
                _fl_lk   = _line_key(_fl, _fl_idx)
                _fl_sku  = str(_fl.get("batch_no") or (_fl.get("lens_params") or {}).get("batch_no") or "")
                _fl_col  = str((_fl.get("lens_params") or {}).get("colour_mix")
                                or _fl.get("colour_mix") or "")
                _fl_qty  = int(_fl.get("billing_qty") or 0)
                _fl_alloc= int(_fl.get("allocated_qty") or 0)
                # manufacturing_route decoded from lens_params by order_loader.
                # If still blank but batch_no is set → infer STOCK (frame picked from inventory).
                _fl_route= str((_fl.get("manufacturing_route") or
                                assignments.get(_fl_lk,{}).get("route") or
                                (ROUTE_STOCK if _fl_sku else ROUTE_VENDOR))).upper()
                _fl_supp = str(assignments.get(_fl_lk,{}).get("supplier_name")
                               or _fl.get("supplier_name") or "")

                # ── Auto-confirm with saved route ─────────────────────
                if _fl_lk not in assignments:
                    # manufacturing_route decoded from lens_params by order_loader.
                    # batch_no on the line = frame was picked from inventory = STOCK.
                    _lp_fl = _fl.get("lens_params") or {}
                    _lp_fl = _lp_fl if isinstance(_lp_fl, dict) else {}
                    _fl_bno = str(_fl.get("batch_no") or _lp_fl.get("batch_no") or "").strip()
                    _inferred_route = _fl_route
                    if not _inferred_route and _fl_bno:
                        _inferred_route = ROUTE_STOCK
                    _auto_route = _inferred_route if _inferred_route in (ROUTE_STOCK, ROUTE_VENDOR) else (
                        ROUTE_STOCK if _fl_bno else ROUTE_VENDOR
                    )
                    assignments[_fl_lk] = {
                        "route":    _auto_route,
                        "confirmed": _auto_route == ROUTE_STOCK,
                    }
                elif not assignments[_fl_lk].get("confirmed") and assignments[_fl_lk].get("route") == ROUTE_STOCK:
                    assignments[_fl_lk]["confirmed"] = True

                # Use assignment route as canonical (may differ from line if user just changed it)
                _cur_asgn_route = str(assignments.get(_fl_lk, {}).get("route") or _fl_route).upper()

                # ── Status chip ───────────────────────────────────────
                # A frame is "from stock" when:
                #   a) route confirmed as STOCK (from bo_assignments or manufacturing_route), OR
                #   b) batch_no/SKU was set at punching (allocated_qty may still be 0 pre-save)
                _is_stock_confirmed = (
                    _cur_asgn_route == ROUTE_STOCK
                    and (_fl_alloc >= _fl_qty or bool(_fl_sku))
                )

                if _is_stock_confirmed:
                    # Auto-confirm silently — no button needed
                    if not assignments.get(_fl_lk, {}).get("confirmed"):
                        assignments[_fl_lk] = {"route": ROUTE_STOCK, "confirmed": True}

                    # Green stock chip
                    _stock_sub = (f"SKU: {_fl_sku}" if _fl_sku else "")
                    _stock_sub += (f"  · {_fl_col}" if _fl_col else "")
                    if _fl_alloc >= _fl_qty:
                        _stock_sub = f"✅ {_fl_alloc}/{_fl_qty} pcs · 📦 Stock" + (f"  · {_stock_sub}" if _stock_sub else "")
                    else:
                        _stock_sub = f"📦 From Stock · {_stock_sub} · pending save"
                    st.markdown(
                        f"<div style='background:#0a1f0a;border:1px solid #22c55e44;"
                        f"border-radius:6px;padding:6px 12px'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                        f"<span style='color:#a78bfa;font-size:0.82rem;font-weight:700'>"
                        f"🖼 {grp['product_name'].split(' | ')[0]}</span>"
                        f"<span style='color:#86efac;font-size:0.72rem'>{_stock_sub}"
                        f"</span></div></div>",
                        unsafe_allow_html=True
                    )
                    # Stock availability info
                    try:
                        from modules.batch_manager import get_frame_stock
                        _fsk_df = get_frame_stock(str(_fl.get("product_id") or ""))
                        if not _fsk_df.empty:
                            _avail_total = int(_fsk_df["available_qty"].sum())
                            st.caption(
                                f"📦 Stock available: {_avail_total} pcs"
                                + (f" · SKU: `{_fl_sku}`" if _fl_sku else "")
                            )
                    except Exception:
                        pass

                elif _cur_asgn_route == ROUTE_STOCK and not _fl_sku:
                    # Route is STOCK but no SKU/batch — show a soft warning + radio
                    st.warning("📦 Route set to Stock but no SKU found — confirm or switch to Supplier")
                    _frame_routes = [ROUTE_STOCK, ROUTE_VENDOR]
                    _frame_lbls   = {ROUTE_VENDOR: "🏭 Supplier (Direct)", ROUTE_STOCK: "📦 From Stock"}
                    _new_route = st.radio(
                        "Route",
                        _frame_routes,
                        index=0,
                        format_func=lambda x: _frame_lbls.get(x, x),
                        horizontal=True,
                        key=f"frame_route_{_fl_lk}",
                        label_visibility="collapsed",
                    )
                    if _new_route == ROUTE_VENDOR:
                        _render_supplier_selector(_fl, _fl_idx, _fl_lk, assignments, oid, label="Supplier")
                        if assignments.get(_fl_lk, {}).get("supplier_id"):
                            assignments[_fl_lk]["confirmed"] = True
                    else:
                        assignments[_fl_lk] = {"route": ROUTE_STOCK, "confirmed": True}
                    continue

                else:
                    # VENDOR — show amber chip + supplier selector
                    _supp_label = str(assignments.get(_fl_lk, {}).get("supplier_name")
                                      or _fl.get("supplier_name") or "")
                    st.markdown(
                        f"<div style='background:#1a1000;border:1px solid #f59e0b44;"
                        f"border-radius:6px;padding:6px 12px'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                        f"<span style='color:#a78bfa;font-size:0.82rem;font-weight:700'>"
                        f"🖼 {grp['product_name'].split(' | ')[0]}</span>"
                        f"<span style='color:#fcd34d;font-size:0.72rem'>"
                        f"🏭 {'Via: '+_supp_label if _supp_label else 'Assign Supplier'}"
                        f"{'  · SKU: '+_fl_sku if _fl_sku else ''}"
                        f"</span></div></div>",
                        unsafe_allow_html=True
                    )
                    # Route radio — lets operator switch to Stock if they have inventory
                    _frame_routes = [ROUTE_VENDOR, ROUTE_STOCK]
                    _frame_lbls   = {ROUTE_VENDOR: "🏭 Supplier (Direct)", ROUTE_STOCK: "📦 From Stock"}
                    _cur_r = _cur_asgn_route if _cur_asgn_route in _frame_routes else ROUTE_VENDOR
                    _new_route = st.radio(
                        "Route",
                        _frame_routes,
                        index=_frame_routes.index(_cur_r),
                        format_func=lambda x: _frame_lbls.get(x, x),
                        horizontal=True,
                        key=f"frame_route_{_fl_lk}",
                        label_visibility="collapsed",
                    )
                    if _new_route == ROUTE_VENDOR:
                        _render_supplier_selector(_fl, _fl_idx, _fl_lk, assignments, oid, label="Supplier")
                        if assignments.get(_fl_lk, {}).get("supplier_id"):
                            assignments[_fl_lk]["confirmed"] = True
                    else:
                        # Operator switched to Stock — auto-confirm (no extra button)
                        _apply_shift(
                            line=_fl, new_route=ROUTE_STOCK,
                            supplier_id=None, supplier_name=None,
                            order=order, lk=_fl_lk,
                        )
                        assignments[_fl_lk] = {"route": ROUTE_STOCK, "confirmed": True}
                        st.rerun()
                    continue  # skip expander below for VENDOR path

                # ── For STOCK frames: show route-change expander ──────
                with st.expander("🔄 Change Route / Supplier", expanded=False):
                    _frame_routes2 = [ROUTE_STOCK, ROUTE_VENDOR]
                    _frame_lbls2   = {ROUTE_STOCK: "📦 Stock", ROUTE_VENDOR: "🏭 Supplier (Direct)"}
                    _chg_route = st.radio(
                        "Fulfillment",
                        _frame_routes2,
                        index=0,  # currently STOCK, so index 0
                        format_func=lambda x: _frame_lbls2.get(x, x),
                        horizontal=True,
                        key=f"frame_route_chg_{_fl_lk}",
                    )
                    if _chg_route == ROUTE_VENDOR:
                        _render_supplier_selector(_fl, _fl_idx, _fl_lk, assignments, oid,
                                                  label="Supplier")
                    if _chg_route != ROUTE_STOCK:
                        _apply_shift(
                            line=_fl, new_route=_chg_route,
                            supplier_id=assignments.get(_fl_lk, {}).get("supplier_id"),
                            supplier_name=assignments.get(_fl_lk, {}).get("supplier_name"),
                            order=order, lk=_fl_lk,
                        )
                        assignments[_fl_lk] = {
                            "route":         _chg_route,
                            "confirmed":     True,
                            "supplier_id":   assignments.get(_fl_lk, {}).get("supplier_id", ""),
                            "supplier_name": assignments.get(_fl_lk, {}).get("supplier_name", ""),
                        }
                        st.rerun()

            continue  # Skip full lens card rendering for frames

        # ── Full card for lens R/L lines ─────────────────────────
        st.markdown(f"#### 👁️ {grp['product_name'].split(' | ')[0]}")

        if has_r and has_l:
            col_r, col_l = st.columns(2)
        elif has_r:
            col_r = st.container()
            col_l = None
        else:
            col_r = None
            col_l = st.container()

        for eye_label, line, idx, col in [
            ("R", grp["R"], grp["R_idx"], col_r),
            ("L", grp["L"], grp["L_idx"], col_l),
        ]:
            if line is None:
                continue

            lk  = _line_key(line, idx)
            pwr = _power_str(line)
            qty = line.get("billing_qty", 1)
            _real_eye = str(line.get("eye_side", "")).upper()
            if _real_eye in ("R", "RIGHT"):
                eye_title = "👁 RIGHT EYE"
            elif _real_eye in ("L", "LEFT"):
                eye_title = "👁 LEFT EYE"
            else:
                eye_title = "🖼 FRAME / ITEM"

            with col:
                with st.container(border=True):
                    hcol1, hcol2 = st.columns([5, 1])
                    with hcol1:
                        st.markdown(
                            f"<div style='font-weight:700;color:#94a3b8;"
                            f"font-size:0.8rem'>{eye_title}</div>"
                            f"<div style='color:#e2e8f0;font-size:0.85rem'>"
                            f"{grp['product_name'].split(' | ')[0]}"
                            + (f"  <code style='font-size:0.75rem'>{pwr}</code>" if pwr else "")
                            + "</div>",
                            unsafe_allow_html=True
                        )
                    with hcol2:
                        st.caption(f"×{qty}")

                    # ── CASE 1: Stock-allocated ───────────────────────
                    if _is_stock_allocated(line):
                        alloc_qty = int(line.get("allocated_qty") or 0)
                        st.success(f"📦 Stock — {alloc_qty} allotted")
                        with st.expander("⚙️ Shift route?", expanded=False):
                            _render_shift_inline(line, idx, lk, order, all_lines, oid)
                        continue

                    # ── CASE 2: RX order ─────────────────────────────
                    if _is_rx(line):
                        st.info("📋 **RX Order** — always fulfilled by Supplier")
                        _render_supplier_selector(line, idx, lk, assignments, oid, force=True)
                        continue

                    # ── CASE 3: Normal line — smart route radio ────────
                    # Routes available depend on product type:
                    #   Ophthalmic lens → Supplier / In-house / External Lab (no Stock)
                    #   Frame / accessory / solution → Stock / Supplier (no Job Card)
                    #   Everything else → all 4 routes
                    _avail_routes, _default_route = _get_job_card_routes_for_line(line)

                    current_route = assignments.get(lk, {}).get(
                        "route", line.get("manufacturing_route", _default_route)
                    )
                    # If saved route not in available routes for this type, use default
                    if current_route not in _avail_routes:
                        current_route = _default_route

                    route_labels = [ROUTE_LABELS[r] for r in _avail_routes]
                    try:
                        default_idx = _avail_routes.index(current_route)
                    except ValueError:
                        default_idx = 0

                    # For ophthalmic lenses: show a clear job card label
                    _radio_label = "Route"
                    if _is_ophthalmic_lens(line):
                        _radio_label = "🔬 Fulfillment Route"
                    elif _is_stock_only_product(line):
                        _radio_label = "📦 Fulfillment"

                    chosen_label = st.radio(
                        _radio_label,
                        route_labels,
                        index=default_idx,
                        horizontal=True,
                        key=f"route_radio_{lk}_{oid}",
                        label_visibility="collapsed",
                    )
                    chosen_route = _avail_routes[route_labels.index(chosen_label)]

                    if lk not in assignments:
                        assignments[lk] = {}
                    assignments[lk]["route"] = chosen_route

                    if chosen_route == ROUTE_VENDOR:
                        _render_supplier_selector(line, idx, lk, assignments, oid)

                    elif chosen_route == ROUTE_INHOUSE:
                        note = st.text_input(
                            "Job notes (optional)",
                            value=assignments[lk].get("job_notes", ""),
                            key=f"job_note_{lk}_{oid}",
                            placeholder="Technician, special instructions…",
                        )
                        assignments[lk]["job_notes"] = note
                        assignments[lk]["job_type"]  = "INHOUSE"

                    elif chosen_route == ROUTE_EXTERNAL_LAB:
                        _render_supplier_selector(
                            line, idx, lk, assignments, oid, label="External Lab / Supplier"
                        )
                        assignments[lk]["job_type"] = "EXTERNAL_LAB"

                    elif chosen_route == ROUTE_STOCK:
                        # ── Expiry-tracked products (solutions, cleaners, drops):
                        #    operator MUST pick a batch+expiry — no silent auto-alloc ──
                        if _requires_batch_expiry(line):
                            assignments[lk]["job_type"] = "STOCK"
                            _render_expiry_batch_selector(line, idx, lk, assignments, oid)
                        else:
                            # ── Non-expiry stock (ophthalmic lenses, frames, accessories):
                            #    auto-allocate via batch_manager FIFO and confirm silently ──
                            pid_stock = str(line.get("product_id") or "")
                            if pid_stock:
                                try:
                                    from modules.batch_manager import (
                                        get_batches_fifo, allocate_batches_fifo
                                    )
                                    _sph = line.get("sph"); _cyl = line.get("cyl")
                                    _ax  = line.get("axis"); _add = line.get("add_power")
                                    # Frames/accessories: eye_side=None so batch_manager
                                    # routes to get_frame_stock() which ignores eye_side
                                    _raw_eye = str(line.get("eye_side") or "").upper() or None
                                    _eye = None if _is_stock_only_product(line) else _raw_eye
                                    _batches = get_batches_fifo(
                                        pid_stock, sph=_sph, cyl=_cyl,
                                        axis=_ax, add_power=_add, eye_side=_eye
                                    )
                                    if _batches.empty:
                                        st.warning("📦 No stock available — switching to Supplier")
                                        assignments[lk]["route"] = ROUTE_VENDOR
                                    else:
                                        _total_stock = int(
                                            _batches["available_qty"].sum()
                                            if "available_qty" in _batches.columns
                                            else 0
                                        )
                                        _need_qty  = int(line.get("billing_qty") or 1)
                                        _allocated = allocate_batches_fifo(_batches, _need_qty)
                                        _alloc_recs = []
                                        for _, _br in _allocated.iterrows():
                                            _aq = float(_br.get("allocated_qty", 0))
                                            if _aq > 0:
                                                _alloc_recs.append({
                                                    "batch_no":      str(_br.get("batch_no", "") or ""),
                                                    "allocated_qty": int(_aq),
                                                    "selling_price": float(_br.get("selling_price", 0) or 0),
                                                })
                                        if _alloc_recs:
                                            assignments[lk]["batch_allocation"] = _alloc_recs
                                            assignments[lk]["route"]    = ROUTE_STOCK
                                            assignments[lk]["job_type"] = "STOCK"
                                            st.success(
                                                f"📦 {_total_stock} in stock — "
                                                f"{_need_qty} auto-allocated ✅"
                                            )
                                        else:
                                            st.warning("📦 No allocatable qty — switching to Supplier")
                                            assignments[lk]["route"] = ROUTE_VENDOR
                                except Exception as _se:
                                    st.info(f"📦 From Stock (batch lookup unavailable: {_se})")
                                    assignments[lk]["route"]    = ROUTE_STOCK
                                    assignments[lk]["job_type"] = "STOCK"
                            else:
                                st.warning("No product ID — cannot check stock")

        st.markdown("---")

    st.session_state.bo_assignments = assignments


def _render_expiry_batch_selector(
    line: Dict,
    idx: int,
    lk: str,
    assignments: dict,
    oid: str,
) -> bool:
    """
    Batch + expiry selector for products that carry expiry dates
    (solutions, cleaners, eye drops, medicines).

    Shows batches from inventory_stock ordered by expiry_date ASC (FEFO).
    Operator MUST pick a batch — saving is blocked until one is confirmed.

    Returns True if a valid batch with expiry is selected, False otherwise.
    This return value is used to block the Confirm All button.
    """
    product_id = str(line.get("product_id") or "")
    needed_qty = int(line.get("billing_qty") or line.get("quantity") or 1)

    # ── Load batches FEFO order ────────────────────────────────────────
    batches = []
    try:
        from modules.sql_adapter import run_query as _rq_exp
        batches = _rq_exp("""
            SELECT
                batch_no,
                COALESCE(expiry_date::text, '')    AS expiry_date,
                COALESCE(mfg_date::text, '')       AS mfg_date,
                (quantity - COALESCE(allocated_qty, 0)) AS available_qty,
                quantity,
                COALESCE(mrp, selling_price, 0)::numeric AS mrp,
                COALESCE(selling_price, mrp, 0)::numeric AS selling_price
            FROM inventory_stock
            WHERE product_id = %(pid)s::uuid
              AND (quantity - COALESCE(allocated_qty, 0)) > 0
              AND COALESCE(is_active, true) = true
            ORDER BY
                CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END,
                expiry_date ASC,
                batch_no ASC
        """, {"pid": product_id}) or []
        batches = [dict(r) for r in batches]
    except Exception as _be:
        st.warning(f"⚠️ Could not load batches: {_be}")

    if not batches:
        st.error(
            "🚫 **No stock with expiry found.** "
            "Add inventory with batch/expiry details before allocating this product."
        )
        # Mark assignment as VENDOR fallback so it doesn't silently block save
        if lk not in assignments:
            assignments[lk] = {}
        assignments[lk]["route"]     = ROUTE_VENDOR
        assignments[lk]["confirmed"] = False
        return False

    # ── Build display labels: Batch | Expiry | Available ──────────────
    def _batch_label(r) -> str:
        parts = [str(r.get("batch_no") or "—")]
        exp = str(r.get("expiry_date") or "").strip()
        if exp:
            # Highlight near-expiry (≤ 90 days)
            try:
                from datetime import date as _date, datetime as _dt
                _exp_d = _dt.strptime(exp[:10], "%Y-%m-%d").date()
                days_left = (_exp_d - _date.today()).days
                if days_left < 0:
                    parts.append(f"⛔ EXPIRED ({exp[:10]})")
                elif days_left <= 30:
                    parts.append(f"🔴 Exp: {exp[:10]} ({days_left}d left)")
                elif days_left <= 90:
                    parts.append(f"🟡 Exp: {exp[:10]} ({days_left}d left)")
                else:
                    parts.append(f"✅ Exp: {exp[:10]}")
            except Exception:
                parts.append(f"Exp: {exp[:10]}")
        else:
            parts.append("⚠️ No expiry date")
        parts.append(f"Avail: {int(r.get('available_qty') or 0)}")
        return " | ".join(parts)

    batch_ids    = [str(r["batch_no"]) for r in batches]
    batch_labels = {str(r["batch_no"]): _batch_label(r) for r in batches}
    batch_map    = {str(r["batch_no"]): r for r in batches}

    # ── Restore previously selected batch if any ──────────────────────
    _saved_batch = assignments.get(lk, {}).get("batch_no") or ""
    _def_idx = batch_ids.index(_saved_batch) if _saved_batch in batch_ids else 0

    st.markdown(
        "<div style='color:#fbbf24;font-size:0.75rem;font-weight:600;"
        "margin-bottom:4px'>📋 Select Batch & Expiry (required)</div>",
        unsafe_allow_html=True,
    )

    bc1, bc2 = st.columns([3, 2])
    with bc1:
        chosen_batch = st.selectbox(
            "Batch",
            batch_ids,
            index=_def_idx,
            format_func=lambda x: batch_labels.get(x, x),
            key=f"expiry_batch_sel_{lk}_{oid}",
            label_visibility="collapsed",
        )
    with bc2:
        _row = batch_map.get(chosen_batch, {})
        _db_price = float(_row.get("mrp") or _row.get("selling_price") or
                          float(line.get("unit_price") or 0))
        exp_price = st.number_input(
            "Price ₹",
            min_value=0.0,
            value=_db_price,
            step=10.0,
            format="%.2f",
            key=f"expiry_price_{lk}_{oid}",
            label_visibility="collapsed",
        )

    # ── Validation: check expiry and available qty ────────────────────
    _row         = batch_map.get(chosen_batch, {})
    _avail       = int(_row.get("available_qty") or 0)
    _exp_str     = str(_row.get("expiry_date") or "").strip()
    _is_expired  = False
    _is_no_expiry = not _exp_str

    if _exp_str:
        try:
            from datetime import date as _d2, datetime as _dt2
            _exp_d2 = _dt2.strptime(_exp_str[:10], "%Y-%m-%d").date()
            _is_expired = _exp_d2 < _d2.today()
        except Exception:
            pass

    _valid = True
    if _is_no_expiry:
        st.warning("⚠️ This batch has no expiry date recorded — add it in inventory before allocating.")
        _valid = False
    elif _is_expired:
        st.error(f"⛔ Batch `{chosen_batch}` is EXPIRED — do not allocate expired stock.")
        _valid = False
    elif _avail < needed_qty:
        st.warning(
            f"⚠️ Only {_avail} available — need {needed_qty}. "
            "Pick another batch or adjust qty."
        )
        _valid = False

    # ── Write to assignments ──────────────────────────────────────────
    if lk not in assignments:
        assignments[lk] = {}
    assignments[lk]["route"]     = ROUTE_STOCK
    assignments[lk]["batch_no"]  = chosen_batch
    assignments[lk]["expiry_date"] = _exp_str
    assignments[lk]["batch_allocation"] = [{
        "batch_no":      chosen_batch,
        "allocated_qty": needed_qty,
        "selling_price": exp_price,
        "qty":           needed_qty,
    }]
    assignments[lk]["confirmed"] = _valid

    if _valid:
        st.success(
            f"✅ Batch `{chosen_batch}` · Exp: {_exp_str[:10]} · "
            f"{_avail} available · ₹{exp_price:,.2f}"
        )

    return _valid


def _render_supplier_selector(
    line: Dict,
    idx: int,
    lk: str,
    assignments: dict,
    oid: str,
    label: str = "Supplier",
    force: bool = False,
):
    """Ranked supplier dropdown for a line."""
    # Determine route_type for product_supplier_map lookup
    _route_for_sup = str(assignments.get(lk, {}).get("route") or
                         line.get("manufacturing_route") or "VENDOR").upper()
    _psm_route = "EXTERNAL_LAB" if _route_for_sup == "EXTERNAL_LAB" else "VENDOR"

    suppliers = _get_ranked_suppliers_for_product(
        line.get("product_id"), route_type=_psm_route
    )

    if not suppliers:
        st.warning("⚠️ No suppliers found — add suppliers in Party Master or Supplier Mapping")
        return

    # Build display options: primary gets ⭐, mapped alternates get rank, rest unlabelled
    options       = [s["id"] for s in suppliers]
    option_labels = {}
    for s in suppliers:
        name = s.get("name") or s.get("party_name") or s["id"]
        if s.get("is_primary"):
            option_labels[s["id"]] = f"⭐ {name} (Primary)"
        elif s.get("past_orders") and int(s["past_orders"]) > 0:
            option_labels[s["id"]] = f"{name} ({s['past_orders']} orders)"
        else:
            option_labels[s["id"]] = name

    # Auto-select: saved → primary → first in list
    saved_id = assignments.get(lk, {}).get("supplier_id")
    if not saved_id:
        # Auto-fill primary supplier
        primary = next((s for s in suppliers if s.get("is_primary")), None)
        if primary:
            saved_id = primary["id"]
            if lk not in assignments: assignments[lk] = {}
            assignments[lk]["supplier_id"]   = saved_id
            assignments[lk]["supplier_name"] = option_labels.get(saved_id, "")
    try:
        default_idx = options.index(saved_id) if saved_id in options else 0
    except (ValueError, TypeError):
        default_idx = 0

    chosen_id = st.selectbox(
        label,
        options,
        index=default_idx,
        format_func=lambda x: option_labels.get(x, x),
        key=f"supplier_sel_{lk}_{oid}",
    )

    if lk not in assignments:
        assignments[lk] = {}
    assignments[lk]["supplier_id"]   = chosen_id
    assignments[lk]["supplier_name"] = option_labels.get(chosen_id, chosen_id)


# ═══════════════════════════════════════════════════════════════════════
# FRAME SKU EDITOR — change colour/SKU/price after stock allocation
# ═══════════════════════════════════════════════════════════════════════

def _render_frame_sku_editor(
    line: Dict,
    idx: int,
    lk: str,
    order: Dict,
    all_lines: List[Dict],
    oid: str,
) -> None:
    """
    Inline panel for frame lines that are already stock-allocated.
    Allows operator to:
      1. See current SKU / colour / price
      2. Pick a different available SKU for the same product
      3. Override the price (MRP)
      4. Apply — updates line dict + batch_allocation in session
    """
    import pandas as _pd

    product_id  = str(line.get("product_id") or "")
    current_sku = str(line.get("batch_no") or "")
    current_col = str((line.get("lens_params") or {}).get("colour_mix") or line.get("colour_mix") or "")
    current_grp = str((line.get("lens_params") or {}).get("frame_group") or line.get("frame_group") or "")
    current_prc = float((line.get("batch_allocation") or [{}])[0].get("selling_price") or
                        line.get("unit_price") or 0)

    st.caption(
        f"Current: SKU `{current_sku}`"
        + (f" | Colour: **{current_col}**" if current_col else "")
        + (f" | Group: {current_grp}" if current_grp else "")
        + f" | Price: ₹{current_prc:,.2f}"
    )

    # ── Load available SKUs for this product ─────────────────────────
    sku_options = []
    try:
        from modules.sql_adapter import run_query as _rq
        _rows = _rq(
            """SELECT batch_no,
                      COALESCE(frame_group, '') AS frame_group,
                      COALESCE(colour_mix,  '') AS colour_mix,
                      COALESCE(mrp, selling_price, 0)::numeric AS mrp,
                      COALESCE(selling_price, mrp, 0)::numeric AS selling_price,
                      quantity
               FROM inventory_stock
               WHERE product_id::text = %(pid)s
                 AND quantity > 0
                 AND COALESCE(is_active, true) = true
               ORDER BY frame_group, colour_mix, batch_no""",
            {"pid": product_id}
        ) or []
        sku_options = [dict(r) for r in _rows]
    except Exception as _e:
        st.warning(f"Could not load SKUs: {_e}")

    if not sku_options:
        st.info("No alternative SKUs in stock for this product.")
        with st.expander("⚙️ Shift route instead?"):
            _render_shift_inline(line, idx, lk, order, all_lines, oid)
        return

    # Build display labels: "SKU | Group | Colour | Qty"
    def _sku_label(r):
        parts = [str(r.get("batch_no") or "")]
        if r.get("frame_group"): parts.append(str(r["frame_group"]))
        if r.get("colour_mix"):  parts.append(str(r["colour_mix"]))
        parts.append(f"Qty:{int(r.get('quantity') or 0)}")
        return " | ".join(parts)

    sku_ids    = [str(r.get("batch_no") or "") for r in sku_options]
    sku_labels = {str(r.get("batch_no") or ""): _sku_label(r) for r in sku_options}
    sku_map    = {str(r.get("batch_no") or ""): r for r in sku_options}

    # Default to current SKU if available, else first
    _def_idx = sku_ids.index(current_sku) if current_sku in sku_ids else 0

    ec1, ec2 = st.columns([3, 2])
    with ec1:
        chosen_sku = st.selectbox(
            "Select SKU",
            sku_ids,
            index=_def_idx,
            format_func=lambda x: sku_labels.get(x, x),
            key=f"frame_sku_sel_{lk}_{oid}",
        )
    with ec2:
        _chosen_row = sku_map.get(chosen_sku, {})
        _db_price   = float(_chosen_row.get("mrp") or _chosen_row.get("selling_price") or current_prc)
        new_price = st.number_input(
            "Price ₹ (MRP)",
            min_value=0.0,
            value=_db_price if _db_price > 0 else current_prc,
            step=50.0,
            format="%.2f",
            key=f"frame_price_{lk}_{oid}",
            help="Edit if DB price is wrong or differs from agreed rate",
        )

    # Show new colour/group info
    _new_col = str(_chosen_row.get("colour_mix") or "")
    _new_grp = str(_chosen_row.get("frame_group") or "")
    if _new_col or _new_grp:
        st.caption(
            "New: "
            + (" | ".join(filter(None, [_new_grp, _new_col])))
        )

    if new_price == 0:
        st.warning("⚠️ Price is ₹0 — enter the frame price before applying")

    ac1, ac2 = st.columns([1, 1])
    with ac1:
        if st.button(
            "✅ Apply Change",
            key=f"frame_apply_{lk}_{oid}",
            type="primary",
            disabled=(new_price == 0),
        ):
            # ── Mutate line in-place ──────────────────────────────────
            line["batch_no"]    = chosen_sku
            line["unit_price"]  = new_price
            line["total_price"] = new_price * float(line.get("billing_qty") or 1)
            line["colour_mix"]  = _new_col
            line["frame_group"] = _new_grp

            # Update lens_params so display + persistence stay consistent
            _lp = dict(line.get("lens_params") or {})
            _lp["batch_no"]    = chosen_sku
            _lp["colour_mix"]  = _new_col
            _lp["frame_group"] = _new_grp
            line["lens_params"] = _lp

            # Rebuild batch_allocation with new SKU + price
            line["batch_allocation"] = [{
                "batch_no":      chosen_sku,
                "allocated_qty": int(line.get("billing_qty") or 1),
                "selling_price": new_price,
                "qty":           int(line.get("billing_qty") or 1),
            }]

            # Rebuild display name: base_name | SKU | Group | Colour
            _base = str(line.get("product_name") or "").split(" | ")[0]
            _name_parts = [p for p in [_base, chosen_sku, _new_grp, _new_col] if p]
            line["product_name"] = " | ".join(_name_parts)

            # Keep assignment state consistent
            assignments = st.session_state.get("bo_assignments", {})
            if lk not in assignments:
                assignments[lk] = {}
            # Fix 5: Reverse old stock allocation before applying new SKU
            # This ensures inventory_stock.allocated_qty stays correct
            if current_sku and current_sku != chosen_sku:
                try:
                    from modules.sql_adapter import run_write as _rw_alloc_rev
                    _old_qty = int(line.get("billing_qty") or 1)
                    _old_pid = str(line.get("product_id") or "")
                    if _old_pid and _old_qty > 0:
                        # Release old allocation (reduce allocated_qty)
                        _rw_alloc_rev("""
                            UPDATE inventory_stock
                            SET allocated_qty = GREATEST(0, COALESCE(allocated_qty,0) - %(qty)s)
                            WHERE product_id = %(pid)s::uuid
                              AND batch_no   = %(bno)s
                        """, {"pid": _old_pid, "bno": current_sku, "qty": _old_qty})
                        # Reserve new allocation
                        _rw_alloc_rev("""
                            UPDATE inventory_stock
                            SET allocated_qty = COALESCE(allocated_qty,0) + %(qty)s
                            WHERE product_id = %(pid)s::uuid
                              AND batch_no   = %(bno)s
                        """, {"pid": _old_pid, "bno": chosen_sku, "qty": _old_qty})
                except Exception as _alloc_e:
                    import logging
                    logging.warning(f"[Frame SKU] Allocation swap failed: {_alloc_e}")

            # Audit log the SKU change
            try:
                from modules.backoffice.audit_logger import audit, AuditAction
                from modules.security.roles import current_user
                _user = (current_user() or {}).get("name", "backoffice")
                audit(
                    AuditAction.PRODUCT_CHANGED,
                    entity    = "order_lines",
                    entity_id = str(line.get("line_id") or line.get("id") or ""),
                    order_id  = oid,
                    user_id   = _user,
                    payload   = {
                        "action":    "frame_sku_changed",
                        "old_sku":   current_sku,
                        "new_sku":   chosen_sku,
                        "old_price": current_prc,
                        "new_price": new_price,
                        "colour":    _new_col,
                    }
                )
            except Exception:
                pass

            assignments[lk]["route"]    = ROUTE_STOCK
            assignments[lk]["job_type"] = "STOCK"
            assignments[lk]["batch_allocation"] = line["batch_allocation"]
            st.session_state.bo_assignments = assignments

            st.success(
                f"✅ Updated — SKU: `{chosen_sku}`"
                + (f" | {_new_col}" if _new_col else "")
                + f" | ₹{new_price:,.2f}"
            )
            st.rerun()

    with ac2:
        # Still allow full route shift if needed (e.g. not in stock at all)
        with st.expander("⚙️ Shift route instead?"):
            _render_shift_inline(line, idx, lk, order, all_lines, oid)


# ═══════════════════════════════════════════════════════════════════════
# SHIFT ROUTE — the key feature: move line without re-opening save window
# ═══════════════════════════════════════════════════════════════════════

def _render_shift_inline(
    line: Dict,
    idx: int,
    lk: str,
    order: Dict,
    all_lines: List[Dict],
    oid: str,
):
    """
    Inline shift panel — appears inside an expander on any confirmed line.
    Lets operator shift STOCK→VENDOR, VENDOR→INHOUSE, etc.
    Takes effect immediately (updates line dict + re-categorises order).
    No save required — shift is a live in-session change.
    """
    current = line.get("manufacturing_route", ROUTE_VENDOR)
    st.caption(f"Current route: **{ROUTE_LABELS.get(current, current)}**")

    # All routes including STOCK (so they can shift back to stock if re-allocated)
    all_routes  = [ROUTE_VENDOR, ROUTE_INHOUSE, ROUTE_EXTERNAL_LAB, ROUTE_STOCK]
    all_labels  = [ROUTE_LABELS[r] for r in all_routes]
    try:
        def_idx = all_routes.index(current)
    except ValueError:
        def_idx = 0

    new_label = st.radio(
        "Shift to",
        all_labels,
        index=def_idx,
        horizontal=True,
        key=f"shift_radio_{lk}_{oid}",
    )
    new_route = all_routes[all_labels.index(new_label)]

    supplier_id   = None
    supplier_name = None

    if new_route in (ROUTE_VENDOR, ROUTE_EXTERNAL_LAB):
        suppliers = _get_ranked_suppliers_for_product(line.get("product_id"))
        if suppliers:
            opts   = [s["id"] for s in suppliers]
            olabels = {s["id"]: s["name"] for s in suppliers}
            sid = st.selectbox(
                "Supplier",
                opts,
                format_func=lambda x: olabels.get(x, x),
                key=f"shift_supplier_{lk}_{oid}",
            )
            supplier_id   = sid
            supplier_name = olabels.get(sid, sid)

    if st.button(
        f"⚡ Apply Shift",
        key=f"apply_shift_{lk}_{oid}",
        type="primary",
    ):
        _apply_shift(
            line=line,
            new_route=new_route,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            order=order,
            lk=lk,
        )
        st.success(f"✅ Shifted to {ROUTE_LABELS[new_route]}")
        st.rerun()


def _apply_shift(
    line: Dict,
    new_route: str,
    supplier_id: Optional[str],
    supplier_name: Optional[str],
    order: Dict,
    lk: str,
):
    """
    Mutate the line dict and re-categorise order.
    If shifting away from STOCK: clear batch_allocation so stock is freed.
    If shifting to STOCK: only allowed if batch already allocated elsewhere.
    """
    old_route = line.get("manufacturing_route")

    # If leaving stock — clear allocation so inventory is not double-counted
    if old_route == ROUTE_STOCK and new_route != ROUTE_STOCK:
        line["batch_allocation"] = []
        line["allocated_qty"]    = 0
        line["batch_status"]     = "PENDING"

    line["manufacturing_route"] = new_route

    if supplier_id:
        line["supplier_id"]   = supplier_id
        line["supplier_name"] = supplier_name

    # Update assignment state so the panel reflects the change
    assignments = st.session_state.get("bo_assignments", {})
    assignments[lk] = {
        "route":         new_route,
        "supplier_id":   supplier_id,
        "supplier_name": supplier_name,
        "confirmed":     True,
    }
    st.session_state.bo_assignments = assignments

    # Re-categorise so stock_lines / inhouse_lines / lab_order_lines stay correct
    # Rebuild order["lines"] from all three buckets first
    all_lines = (
        order.get("stock_lines", [])
        + order.get("inhouse_lines", [])
        + order.get("lab_order_lines", [])
    )
    order["lines"] = all_lines
    categorize_order_lines(order)


# ═══════════════════════════════════════════════════════════════════════
# LOCKED SUMMARY (after Confirm All)
# ═══════════════════════════════════════════════════════════════════════

def _render_locked_summary(order: Dict, all_lines: List[Dict], oid: str):
    """Compact read-only summary when assignments are locked."""
    assignments = st.session_state.bo_assignments

    for idx, line in enumerate(all_lines):
        lk   = _line_key(line, idx)
        eye  = _eye_badge(line)
        name = line.get("product_name", "Unknown")
        pwr  = _power_str(line)

        asgn  = assignments.get(lk, {})
        route = asgn.get("route") or line.get("manufacturing_route", ROUTE_VENDOR)
        sup   = asgn.get("supplier_name", "")
        icon  = ROUTE_LABELS.get(route, route)

        col_prod, col_route, col_shift = st.columns([4, 3, 1])
        with col_prod:
            st.write(f"**{eye}** {name}" + (f"  `{pwr}`" if pwr else ""))
        with col_route:
            detail = f" — {sup}" if sup else ""
            st.write(f"{icon}{detail}")
        with col_shift:
            # Quick shift button — unlocks just this line
            if st.button("⇄", key=f"quick_shift_{lk}_{oid}", help="Shift this line's route"):
                st.session_state.bo_shift_target = lk
                _safe_unlock(all_lines)  # no-op if job card saved
                st.rerun()

    # ── Shift modal (appears when a specific line's ⇄ was clicked) ───
    shift_target = st.session_state.get("bo_shift_target")
    if shift_target and not st.session_state.bo_assignments_locked:
        for idx, line in enumerate(all_lines):
            if _line_key(line, idx) == shift_target:
                st.markdown("---")
                st.markdown(f"#### ⇄ Shift route — {line.get('product_name','')}")
                _render_shift_inline(line, idx, shift_target, order, all_lines, oid)
                if st.button("Cancel shift", key=f"cancel_shift_{oid}"):
                    st.session_state.bo_shift_target = None
                    st.session_state.bo_assignments_locked = True
                    st.rerun()
                break


# ═══════════════════════════════════════════════════════════════════════
# APPLY ALL ASSIGNMENTS TO LINE DICTS
# ═══════════════════════════════════════════════════════════════════════

def _apply_all_assignments(order: Dict, all_lines: List[Dict]):
    """
    Push all assignment decisions from session_state into the line dicts,
    then re-categorise order so tabs stay consistent.
    ALSO writes manufacturing_route directly to lens_params in DB immediately
    so routes persist without requiring the full order save.
    """
    assignments = st.session_state.bo_assignments

    for idx, line in enumerate(all_lines):
        lk   = _line_key(line, idx)
        asgn = assignments.get(lk)

        if not asgn:
            continue

        route = asgn.get("route")
        if route:
            line["manufacturing_route"] = route

        if route == ROUTE_STOCK:
            # Prefer assignment panel's batch_allocation; fall back to
            # whatever the allocation window already saved on the line dict
            _ba = asgn.get("batch_allocation") or line.get("batch_allocation") or []
            if _ba:
                line["batch_allocation"] = _ba
                line["allocated_qty"]    = sum(int(a.get("allocated_qty", 0)) for a in _ba
                                               if isinstance(a, dict))
                line["batch_status"]     = "ALLOCATED"
                line["order_qty"]        = 0
            elif line.get("allocated_qty", 0) > 0:
                # Line already has allocated_qty from allocation window — preserve it
                line["batch_status"] = "ALLOCATED"
                line["order_qty"]    = 0

        if asgn.get("supplier_id"):
            line["supplier_id"]   = asgn["supplier_id"]
            line["supplier_name"] = asgn.get("supplier_name", "")

        if asgn.get("job_notes"):
            line["job_notes"] = asgn["job_notes"]

        asgn["confirmed"] = True

        # ── Write route to DB immediately ──────────────────────────────
        # Don't wait for full order save — routes must survive page navigation
        if route:
            _lid = str(line.get("line_id") or line.get("id") or "")
            if _lid and len(_lid) > 10:
                try:
                    import json as _json
                    from modules.sql_adapter import run_write as _rw_asgn, run_query as _rq_asgn
                    # Fetch current lens_params from DB (authoritative)
                    _lp_row = _rq_asgn(
                        "SELECT COALESCE(lens_params,'{}')::text AS lp "
                        "FROM order_lines WHERE id=%(lid)s::uuid LIMIT 1",
                        {"lid": _lid}
                    ) or []
                    _lp = _json.loads(_lp_row[0]["lp"]) if _lp_row else {}
                    _lp["manufacturing_route"] = route
                    if asgn.get("supplier_id"):
                        _lp["supplier_id"]   = asgn["supplier_id"]
                        _lp["supplier_name"] = asgn.get("supplier_name","")
                    _rw_asgn(
                        "UPDATE order_lines SET lens_params=%(lp)s::jsonb "
                        "WHERE id=%(lid)s::uuid",
                        {"lp": _json.dumps(_lp), "lid": _lid}
                    )
                except Exception as _ae:
                    import logging as _alog
                    _alog.getLogger(__name__).warning(
                        f"[assignment] immediate DB write failed for {_lid}: {_ae}"
                    )

        # ── Create job_master row for INHOUSE lines immediately ────────────
        # This allows the inhouse pipeline to show the job at JOB_CREATED stage
        # without waiting for full order save.
        if route == ROUTE_INHOUSE and _lid and len(_lid) > 10:
            try:
                from modules.documents.job_card_surfacing import _upsert_job_master
                _upsert_job_master(line, order)
            except Exception as _je:
                import logging as _jlog
                _jlog.getLogger(__name__).warning(
                    f"[assignment] job_master upsert failed for {_lid}: {_je}"
                )

    order["lines"] = all_lines
    categorize_order_lines(order)


# ═══════════════════════════════════════════════════════════════════════
# SUMMARY CHIPS
# ═══════════════════════════════════════════════════════════════════════

def _render_assignment_summary_chips(all_lines: List[Dict], oid: str):
    """Small inline count badges: X Supplier · Y Job · Z Stock."""
    counts = {ROUTE_VENDOR: 0, ROUTE_INHOUSE: 0, ROUTE_EXTERNAL_LAB: 0, ROUTE_STOCK: 0}
    assignments = st.session_state.get("bo_assignments", {})

    for idx, line in enumerate(all_lines):
        lk    = _line_key(line, idx)
        route = assignments.get(lk, {}).get("route") or line.get("manufacturing_route", ROUTE_VENDOR)
        if route in counts:
            counts[route] += 1

    parts = []
    if counts[ROUTE_STOCK]        : parts.append(f"📦 {counts[ROUTE_STOCK]} Stock")
    if counts[ROUTE_VENDOR]       : parts.append(f"🏭 {counts[ROUTE_VENDOR]} Supplier")
    if counts[ROUTE_INHOUSE]      : parts.append(f"🔧 {counts[ROUTE_INHOUSE]} In-house")
    if counts[ROUTE_EXTERNAL_LAB] : parts.append(f"🔬 {counts[ROUTE_EXTERNAL_LAB]} Ext.Lab")

    if parts:
        st.caption("  ·  ".join(parts))
