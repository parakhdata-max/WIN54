"""
Converts DB/pandas/numpy values recursively for PostgreSQL JSON safety.
"""

from decimal import Decimal
import math

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is optional for this helper
    np = None

def sanitize_json(obj):
    if isinstance(obj, Decimal):
        return float(obj)

    if obj is None:
        return None

    if np is not None:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            obj = float(obj)

    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj

    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [sanitize_json(i) for i in obj]

    return obj
