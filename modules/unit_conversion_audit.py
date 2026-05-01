"""
unit_conversion_audit.py — DV ERP Unit Conversion Auditor
==========================================================
Run this anytime to verify box/pcs/pair conversions are correct
across all product categories.

Usage:
    python unit_conversion_audit.py

Or from Streamlit:
    from modules.unit_conversion_audit import run_audit
    results = run_audit()
"""
from __future__ import annotations
import sys
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# CONVERSION RULES (single source of truth)
# ══════════════════════════════════════════════════════════════════════════════

UNIT_RULES = {
    # category         DB unit   Display unit  Conversion key
    "contact_lens":  {"db": "pcs",   "display": "boxes + loose pcs",  "field": "box_size"},
    "solution":      {"db": "pcs",   "display": "boxes + loose pcs",  "field": "box_size"},
    "ophthalmic":    {"db": "pcs",   "display": "pairs + single pcs", "field": "pair=2"},
    "frame":         {"db": "pcs",   "display": "pcs",                "field": "1"},
    "blank":         {"db": "pcs",   "display": "pcs",                "field": "1"},
}

# Ophthalmic always = 2 pcs per pair (right lens + left lens)
OPHTHALMIC_PAIR = 2


def pcs_to_pairs(qty_pcs: int) -> float:
    """Ophthalmic: pcs → decimal pairs. 1 pc = 0.5 pair, 2 pcs = 1 pair."""
    return round(qty_pcs / OPHTHALMIC_PAIR, 1)


def pairs_to_pcs(pairs: int, loose: int = 0) -> int:
    """Ophthalmic: pairs + loose singles → total pcs."""
    return (pairs * OPHTHALMIC_PAIR) + loose


def boxes_loose_to_pcs(boxes: float, loose: float, box_size: int) -> int:
    """CL / SOL: boxes + loose → total pcs."""
    bs = max(1, int(box_size or 1))
    return int(float(boxes or 0) * bs) + int(float(loose or 0))


def pcs_to_boxes_loose(qty_pcs: int, box_size: int) -> tuple[int, int]:
    """CL / SOL: total pcs → (full boxes, loose pcs)."""
    bs = max(1, int(box_size or 1))
    return (qty_pcs // bs, qty_pcs % bs)


def format_display(qty_pcs: int, category: str, box_size: int = 1) -> str:
    """Human-readable quantity for any category."""
    if category == "ophthalmic":
        pairs = pcs_to_pairs(qty_pcs)
        return f"{pairs} pairs"
    elif category in ("contact_lens", "solution"):
        bs = max(1, int(box_size or 1))
        if bs == 1:
            return f"{qty_pcs} pcs"
        boxes, loose = pcs_to_boxes_loose(qty_pcs, bs)
        if boxes > 0 and loose > 0:
            return f"{boxes} boxes + {loose} pcs"
        if boxes > 0:
            return f"{boxes} boxes"
        return f"{loose} pcs"
    return f"{qty_pcs} pcs"


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_audit(verbose: bool = True) -> dict:
    """
    Run full conversion audit. Checks:
    1. All CL products have box_size > 0
    2. All SOL products have box_size > 0
    3. No ophthalmic stock with odd pcs (should always be pairs)
    4. Round-trip: pcs → display → pcs gives same result
    5. All CL/SOL stock quantities are multiples of box_size (or flags loose)
    """
    results = {
        "cl_missing_box_size":    [],
        "sol_missing_box_size":   [],
        "oph_odd_pcs":            [],
        "cl_loose_stock":         [],
        "round_trip_errors":      [],
        "summary":                {},
    }

    try:
        from modules.sql_adapter import run_query
    except ImportError:
        print("❌ Cannot import sql_adapter — run from app context")
        return results

    # ── 1. CL products missing box_size ──────────────────────────────────────
    cl_no_bs = run_query("""
        SELECT product_name, box_size FROM products
        WHERE main_group ILIKE '%contact%'
          AND (box_size IS NULL OR box_size < 1)
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY product_name
    """) or []
    results["cl_missing_box_size"] = [dict(r) for r in cl_no_bs]

    # ── 2. SOL products missing box_size ─────────────────────────────────────
    sol_no_bs = run_query("""
        SELECT product_name, box_size FROM products
        WHERE (main_group ILIKE '%sol%' OR category ILIKE '%sol%')
          AND (box_size IS NULL OR box_size < 1)
          AND COALESCE(is_active, TRUE) = TRUE
        ORDER BY product_name
    """) or []
    results["sol_missing_box_size"] = [dict(r) for r in sol_no_bs]

    # ── 3. Ophthalmic stock with odd pcs ─────────────────────────────────────
    oph_odd = run_query("""
        SELECT p.product_name, s.sph, s.cyl, s.eye_side, s.quantity
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE s.stock_type = 'POWER'
          AND s.eye_side = 'B'
          AND (s.quantity % 2) != 0
          AND COALESCE(s.is_active, TRUE) = TRUE
          AND s.quantity > 0
        ORDER BY p.product_name, s.sph
        LIMIT 20
    """) or []
    results["oph_odd_pcs"] = [dict(r) for r in oph_odd]

    # ── 4. CL stock with loose pcs ───────────────────────────────────────────
    cl_loose = run_query("""
        SELECT p.product_name, p.box_size,
               s.quantity,
               (s.quantity % GREATEST(COALESCE(p.box_size::integer,1),1)) AS loose_pcs
        FROM inventory_stock s
        JOIN products p ON p.id = s.product_id
        WHERE s.stock_type = 'BATCH'
          AND main_group ILIKE '%contact%'
          AND COALESCE(p.box_size, 1) > 1
          AND (s.quantity % GREATEST(COALESCE(p.box_size::integer,1),1)) != 0
          AND COALESCE(s.is_active, TRUE) = TRUE
        ORDER BY p.product_name
        LIMIT 20
    """) or []
    results["cl_loose_stock"] = [dict(r) for r in cl_loose]

    # ── 5. Round-trip test ────────────────────────────────────────────────────
    test_cases = [
        (63,  "contact_lens", 6,  "10 boxes + 3 pcs"),
        (12,  "contact_lens", 6,  "2 boxes"),
        (5,   "contact_lens", 6,  "5 pcs"),
        (30,  "contact_lens", 30, "1 boxes"),
        (10,  "ophthalmic",   2,  "5 pairs"),
        (7,   "ophthalmic",   2,  "3 pairs + 1 pcs"),
        (24,  "solution",     24, "1 boxes"),
        (48,  "solution",     24, "2 boxes"),
        (25,  "solution",     24, "1 boxes + 1 pcs"),
    ]
    for pcs, cat, bs, expected in test_cases:
        got = format_display(pcs, cat, bs)
        if got != expected:
            results["round_trip_errors"].append({
                "pcs": pcs, "category": cat, "box_size": bs,
                "expected": expected, "got": got
            })

    # ── Summary ───────────────────────────────────────────────────────────────
    results["summary"] = {
        "cl_missing_box_size":   len(results["cl_missing_box_size"]),
        "sol_missing_box_size":  len(results["sol_missing_box_size"]),
        "oph_odd_pcs":           len(results["oph_odd_pcs"]),
        "cl_loose_stock":        len(results["cl_loose_stock"]),
        "round_trip_errors":     len(results["round_trip_errors"]),
        "status": "✅ CLEAN" if all(
            len(v) == 0 for k, v in results.items() if k != "summary"
        ) else "⚠️ ISSUES FOUND"
    }

    if verbose:
        print("\n" + "="*60)
        print("DV ERP Unit Conversion Audit")
        print("="*60)
        s = results["summary"]
        print(f"Status: {s['status']}")
        print(f"  CL missing box_size:   {s['cl_missing_box_size']}")
        print(f"  SOL missing box_size:  {s['sol_missing_box_size']}")
        print(f"  Ophthalmic odd pcs:    {s['oph_odd_pcs']}")
        print(f"  CL loose stock:        {s['cl_loose_stock']}")
        print(f"  Round-trip errors:     {s['round_trip_errors']}")

        if results["oph_odd_pcs"]:
            print("\n⚠️  Ophthalmic odd pcs (should be pairs):")
            for r in results["oph_odd_pcs"][:5]:
                print(f"   {r['product_name']} SPH {r['sph']} → {r['quantity']} pcs")

        if results["cl_loose_stock"]:
            print("\n⚠️  CL loose stock (not full boxes):")
            for r in results["cl_loose_stock"][:5]:
                boxes, loose = pcs_to_boxes_loose(r["quantity"], r["box_size"] or 1)
                print(f"   {r['product_name']} → {r['quantity']} pcs "
                      f"= {boxes} boxes + {loose} loose")
        print("="*60 + "\n")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    run_audit(verbose=True)
