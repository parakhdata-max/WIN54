"""
Converts Decimal → float recursively for JSON safety
"""

from decimal import Decimal

def sanitize_json(obj):
    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [sanitize_json(i) for i in obj]

    return obj
