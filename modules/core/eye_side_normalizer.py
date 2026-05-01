"""
modules/core/eye_side_normalizer.py
====================================
Central eye_side normalizer for the entire ERP.

PROBLEM HISTORY:
  Retail punching saved frames as eye_side='OTHER'
  Wholesale punching saved frames as eye_side='OTHER'
  Backoffice grouping expected only R / L / B
  → frames were invisible to grouping → defaulted to VENDOR

CANONICAL VALUES (after normalization):
  'R'       — right eye prescription item
  'L'       — left eye prescription item
  'B'       — both-eye / non-eye-specific item (frames, accessories, CL)
  'SERVICE' — service charge line (fitting, coating, courier etc.)

INPUT VARIANTS HANDLED:
  R, RIGHT                    → R
  L, LEFT                     → L
  B, BOTH, BOTH EYES          → B
  O, OTHER, FRAME, ACCESSORY  → B   ← THE KEY FIX
  S, SVC, SERVICE             → SERVICE
  None, '', NaN, anything else→ B

USAGE:
  from modules.core.eye_side_normalizer import normalize_eye_side

  line["eye_side"] = normalize_eye_side(line.get("eye_side"))

  # In pandas DataFrames:
  df["eye_side"] = df["eye_side"].apply(normalize_eye_side)
"""

from __future__ import annotations


# ── lookup table — O(1), no branching required ──────────────────────────────
_MAP: dict[str, str] = {
    # Right
    "R":         "R",
    "RIGHT":     "R",
    "RE":        "R",
    # Left
    "L":         "L",
    "LEFT":      "L",
    "LE":        "L",
    # Both / non-eye-specific
    "B":         "B",
    "BOTH":      "B",
    "BOTH EYES": "B",
    "BOTHEYES":  "B",
    # ← The critical mappings — these were silently failing grouping
    "O":         "B",
    "OTHER":     "B",
    "FRAME":     "B",
    "FRAMES":    "B",
    "ACCESSORY": "B",
    "ACC":       "B",
    "NA":        "B",
    "N/A":       "B",
    # Service lines — kept distinct so they're never confused with stock items
    "S":         "SERVICE",
    "SVC":       "SERVICE",
    "SERVICE":   "SERVICE",
    "SERVICES":  "SERVICE",
}


def normalize_eye_side(value, *, service_aware: bool = True) -> str:
    """
    Return the canonical eye_side string for *value*.

    Parameters
    ----------
    value:
        Raw eye_side from DB, session state, or UI widget.
        Accepts str, None, float (NaN from pandas), or anything with __str__.
    service_aware:
        If True (default) service lines stay as 'SERVICE'.
        Pass False when you only care about R/L/B (e.g. stock queries).

    Returns
    -------
    str — one of 'R', 'L', 'B', 'SERVICE'
    """
    if value is None:
        return "B"

    # Handle pandas NaN / numpy float NaN
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return "B"
    except Exception:
        pass

    key = str(value).strip().upper()

    if not key:
        return "B"

    result = _MAP.get(key)
    if result is not None:
        if result == "SERVICE" and not service_aware:
            return "B"
        return result

    # Unknown value — default to B (safe fallback)
    return "B"


def normalize_eye_series(series):
    """
    Normalize a pandas Series of eye_side values in place.
    Returns the normalized Series.

    Usage:
        df["eye_side"] = normalize_eye_series(df["eye_side"])
    """
    return series.apply(normalize_eye_side)


# ── display helper — keeps UI labels human-friendly ──────────────────────────
_DISPLAY: dict[str, str] = {
    "R":       "👁️ R",
    "L":       "👁️ L",
    "B":       "👁️👁️ B",
    "SERVICE": "🔧 SVC",
}

def display_eye_side(value) -> str:
    """Return a human-friendly label for UI rendering."""
    return _DISPLAY.get(normalize_eye_side(value), "🔹")
