# modules/workflow/transition_rules.py


# Allowed transitions for line execution

LINE_TRANSITIONS = {

    # Stock
    "ALLOCATED": ["PICKED"],
    "PICKED": ["BILLED"],
    "BILLED": ["DISPATCHED"],
    "DISPATCHED": ["DELIVERED"],
    "DELIVERED": ["CLOSED"],

    # Inhouse
    "JOB_CREATED": ["MATERIAL_ISSUED"],
    "MATERIAL_ISSUED": ["IN_PRODUCTION"],
    "IN_PRODUCTION": ["FINISHING"],
    "FINISHING": ["QC"],
    "QC": ["READY"],
    "READY": ["DISPATCHED"],
    "DISPATCHED": ["CLOSED"],

    # External
    "LAB_ORDERED": ["ACKNOWLEDGED"],
    "ACKNOWLEDGED": ["IN_PROCESS"],
    "IN_PROCESS": ["RECEIVED"],
    "RECEIVED": ["QC"]
}


def can_move(current, target):

    allowed = LINE_TRANSITIONS.get(current, [])

    return target in allowed
