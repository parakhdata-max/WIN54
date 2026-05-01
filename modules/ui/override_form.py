"""
Override handler
"""

def collect_override():

    print("\n⚠️ Override Required")

    reason = input("Enter reason: ").strip()
    approver = input("Approver ID: ").strip()

    if not reason:
        raise ValueError("Override reason mandatory")

    if not approver:
        raise ValueError("Approver ID mandatory")

    return {
        "reason": reason,
        "approver": approver
    }
