"""
procurement/supplier_intelligence.py
======================================
COMPATIBILITY SHIM — canonical module moved to modules/suppliers/intelligence.py

All imports here re-export from the canonical location.
Do not add new code here.
"""
from modules.suppliers.intelligence import (  # noqa: F401
    get_scored_suppliers,
    get_ranked_suppliers_for_assignment,
    get_supplier_scorecard,
    SCORE_WEIGHTS,
    GRADE_MAP,
)
