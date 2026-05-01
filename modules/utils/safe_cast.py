# utils/safe_cast.py
"""
Centralized safe casting helpers
Prevents NaN / None crashes from pandas + JSON reloads
"""

import math


# ---------- CORE CHECK ----------
def _is_nan(v):
    return isinstance(v, float) and math.isnan(v)


# ---------- SAFE FLOAT ----------
def safe_float(v, default=0.0):
    try:
        if v is None or _is_nan(v):
            return default
        return float(v)
    except:
        return default


# ---------- SAFE INT ----------
def safe_int(v, default=0):
    try:
        if v is None or _is_nan(v):
            return default
        return int(v)
    except:
        return default


# ---------- SAFE STR ----------
def safe_str(v, default=""):
    try:
        if v is None or _is_nan(v):
            return default
        return str(v)
    except:
        return default


# ---------- SAFE ROUND ----------
def safe_round(v, digits=2, default=0.0):
    try:
        if v is None or _is_nan(v):
            return default
        return round(float(v), digits)
    except:
        return default