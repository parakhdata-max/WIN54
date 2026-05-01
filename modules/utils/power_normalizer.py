import math

def normalize_power(v):
    """Universal NaN / zero cleaner for optical powers"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if v in ("", 0):
        return None
    return v
