"""
Decision recommendation
"""

def suggest(result):

    if result["errors"]:
        return "REJECT"

    if len(result["warnings"]) > 2:
        return "REVIEW"

    return "APPROVE"
