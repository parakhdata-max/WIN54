"""
cl_pack_utils.py — Contact Lens Pack Size Conversion Utility
=============================================================
Single source of truth for box+loose↔pcs conversion.

DB always stores TOTAL PIECES.
UI shows: Boxes + Loose Pcs

Examples (pack_size=6):
  Upload: 10 boxes + 3 loose → DB stores 63 pcs
  Upload: 12 pcs (loose only) → DB stores 12 pcs → displays 2 boxes 0 loose
  Download: 63 pcs → 10 boxes + 3 loose pcs
  Download: 12 pcs → 2 boxes + 0 loose pcs
  Download: 5 pcs  → 0 boxes + 5 loose pcs
"""
from __future__ import annotations

_box_cache: dict[str, int] = {}


def boxes_loose_to_pcs(boxes: float, loose_pcs: float, pack_size: int) -> int:
    """Convert boxes + loose pcs to total pcs for DB storage."""
    pack = max(1, int(pack_size or 1))
    return int(round(float(boxes or 0) * pack)) + int(float(loose_pcs or 0))


def pcs_to_boxes_loose(qty_pcs: int, pack_size: int) -> tuple[int, int]:
    """
    Split total pcs into (full_boxes, loose_pcs).
    Returns (boxes, loose) — both integers.
    Example: 63 pcs, pack=6 → (10, 3)
    """
    pack = max(1, int(pack_size or 1))
    qty  = max(0, int(qty_pcs or 0))
    return (qty // pack, qty % pack)


def boxes_to_pcs(qty_boxes: float, pack_size: int) -> int:
    """Backward compat — convert boxes only to pcs."""
    return int(round(float(qty_boxes or 0) * max(1, int(pack_size or 1))))


def pcs_to_boxes(qty_pcs: float, pack_size: int) -> float:
    """Backward compat — total pcs to fractional boxes."""
    pack = max(1, int(pack_size or 1))
    return round(float(qty_pcs or 0) / pack, 2)


def format_qty_display(qty_pcs: int, pack_size: int) -> str:
    """Human-readable display: '10 boxes + 3 pcs' or '2 boxes' or '5 pcs'."""
    pack = max(1, int(pack_size or 1))
    if pack == 1:
        return f"{qty_pcs} pcs"
    boxes, loose = pcs_to_boxes_loose(qty_pcs, pack)
    if boxes > 0 and loose > 0:
        return f"{boxes} boxes + {loose} pcs"
    if boxes > 0:
        return f"{boxes} boxes"
    return f"{loose} pcs"


def validate_pack_alignment(qty_pcs: int, pack_size: int) -> dict:
    """Check if stored pcs aligns with pack_size. Returns warning if odd pcs."""
    pack = max(1, int(pack_size or 1))
    if pack == 1:
        return {"aligned": True, "remainder": 0, "warning": ""}
    boxes, loose = pcs_to_boxes_loose(qty_pcs, pack)
    return {
        "aligned":   loose == 0,
        "boxes":     boxes,
        "loose_pcs": loose,
        "warning":   f"⚠️ {qty_pcs} pcs = {boxes} boxes + {loose} loose pcs" if loose else "",
    }


def get_box_size(product_id: str, product_name: str = "") -> int:
    """Get box_size for a product from DB (cached)."""
    if not product_id:
        return _guess_box_from_name(product_name)
    if product_id in _box_cache:
        return _box_cache[product_id]
    try:
        from modules.sql_adapter import run_query
        rows = run_query(
            "SELECT COALESCE(box_size::integer, 1) AS bs FROM products WHERE id=%s::uuid",
            (product_id,)
        ) or []
        bs = int(rows[0]["bs"]) if rows else 1
    except Exception:
        bs = _guess_box_from_name(product_name)
    _box_cache[product_id] = bs
    return bs


def _guess_box_from_name(name: str) -> int:
    n = (name or "").lower()
    if any(x in n for x in ["dailies total", "1-day acuvue", "myday", "clariti 1 day",
                              "precision1", "aspire go dailies"]):
        return 30
    if any(x in n for x in ["air optix", "biofinity", "ultra", "purevision",
                              "acuvue oasys", "avaira", "clariti", "aquasoft"]):
        return 6
    if "10pk" in n:
        return 10
    if "30pk" in n:
        return 30
    if "6pk" in n:
        return 6
    if "3pk" in n:
        return 3
    if "2pk" in n:
        return 2
    return 1


def clear_cache():
    _box_cache.clear()


# ── Ophthalmic Lens: Pair conversion (1 pair = 2 pcs) ────────────────────────
OPHTHALMIC_PAIR_SIZE = 2

def pcs_to_pairs(qty_pcs: int) -> tuple[int, int]:
    """Split ophthalmic pcs into (pairs, odd_singles). 1 pair = 2 pcs."""
    qty = max(0, int(qty_pcs or 0))
    return (qty // 2, qty % 2)

def pairs_to_pcs(pairs: float, odd: float = 0) -> int:
    """Convert pairs + odd singles to pcs for DB storage."""
    return int(round(float(pairs or 0) * 2)) + int(float(odd or 0))

def format_pair_display(qty_pcs: int) -> str:
    """Display ophthalmic qty as pairs + singles. '10 pairs' or '10 pairs + 1 pc'."""
    pairs, odd = pcs_to_pairs(qty_pcs)
    if pairs > 0 and odd > 0:
        return f"{pairs} pairs + {odd} pc"
    if pairs > 0:
        return f"{pairs} pairs"
    return f"{odd} pc"


# ── Solution: same box+loose logic as CL ─────────────────────────────────────
# box_size for solutions comes from products.box_size (default 24 for most solutions)

def get_sol_box_size(product_id: str, product_name: str = "") -> int:
    """Get box_size for a solution product. Default 24 (24 bottles per box)."""
    bs = get_box_size(product_id, product_name)
    return bs if bs > 1 else 24  # default 24 for solutions if not set


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TEST RUNNER — run this file directly to verify all conversions
# python modules/cl_pack_utils.py
# ══════════════════════════════════════════════════════════════════════════════
def run_tests():
    """Verify all pack/pair/box conversions. Prints PASS/FAIL for each."""
    tests = []

    # CL box+loose tests (pack_size=6)
    tests += [
        ("CL: 10 boxes + 3 loose → 63 pcs",    boxes_loose_to_pcs(10, 3, 6) == 63),
        ("CL: 63 pcs → 10 boxes + 3 loose",     pcs_to_boxes_loose(63, 6) == (10, 3)),
        ("CL: 12 pcs → 2 boxes + 0 loose",      pcs_to_boxes_loose(12, 6) == (2, 0)),
        ("CL: 5 pcs → 0 boxes + 5 loose",       pcs_to_boxes_loose(5, 6) == (0, 5)),
        ("CL: 0 boxes + 5 loose → 5 pcs",       boxes_loose_to_pcs(0, 5, 6) == 5),
        ("CL: 2 boxes + 1 loose → 13 pcs",      boxes_loose_to_pcs(2, 1, 6) == 13),
        ("CL: display 63/6",                     format_qty_display(63, 6) == "10 boxes + 3 pcs"),
        ("CL: display 12/6",                     format_qty_display(12, 6) == "2 boxes"),
        ("CL: display 5/6",                      format_qty_display(5, 6) == "5 pcs"),
    ]

    # Ophthalmic pair tests
    tests += [
        ("OPH: 20 pcs → 10 pairs",              pcs_to_pairs(20) == (10, 0)),
        ("OPH: 21 pcs → 10 pairs + 1",          pcs_to_pairs(21) == (10, 1)),
        ("OPH: 10 pairs → 20 pcs",              pairs_to_pcs(10) == 20),
        ("OPH: 10 pairs + 1 → 21 pcs",          pairs_to_pcs(10, 1) == 21),
        ("OPH: display 20 pcs",                  format_pair_display(20) == "10 pairs"),
        ("OPH: display 21 pcs",                  format_pair_display(21) == "10 pairs + 1 pc"),
        ("OPH: display 1 pc",                    format_pair_display(1) == "1 pc"),
    ]

    # Dailies 30pk tests
    tests += [
        ("CL30: 2 boxes 0 loose → 60 pcs",      boxes_loose_to_pcs(2, 0, 30) == 60),
        ("CL30: 65 pcs → 2 boxes + 5 loose",    pcs_to_boxes_loose(65, 30) == (2, 5)),
        ("CL30: display 65/30",                  format_qty_display(65, 30) == "2 boxes + 5 pcs"),
    ]

    # SOL 24pk tests
    tests += [
        ("SOL: 1 box 0 loose → 24 pcs",         boxes_loose_to_pcs(1, 0, 24) == 24),
        ("SOL: 25 pcs → 1 box + 1 loose",       pcs_to_boxes_loose(25, 24) == (1, 1)),
        ("SOL: display 25/24",                   format_qty_display(25, 24) == "1 boxes + 1 pcs"),
    ]

    passed = sum(1 for _, r in tests if r)
    failed = [(name, r) for name, r in tests if not r]

    print(f"\n{'='*55}")
    print(f"  DV ERP Pack Conversion Test Suite")
    print(f"{'='*55}")
    print(f"  Results: {passed}/{len(tests)} PASSED")
    if failed:
        print(f"\n  FAILURES:")
        for name, _ in failed:
            print(f"    ❌ {name}")
    else:
        print(f"  ✅ All tests passed — conversions verified")
    print(f"{'='*55}\n")
    return len(failed) == 0


if __name__ == "__main__":
    run_tests()
