"""
Full Validation Flow Test
=========================
Engine → UI → Decision → Queue
"""

from modules.validators.engine import ValidationEngine

from modules.ui.validation_display import show_result
from modules.ui.decision_helper import suggest
from modules.ui.override_form import collect_override
from modules.ui.validation_queue import add, show


# ----------------------------------
# SAMPLE ORDER DATA
# ----------------------------------

order_data = {
    "order_id": "ORD-001",
    "party_name": "ABC Opticals",
    "product_id": "PROD-101",
    "order_value": 12000,

    "credit_limit": 50000,
    "outstanding": 43000,

    "lines": [
        {
            "product_name": "Single Vision Lens",
            "sph": -2.5,
            "cyl": -1.0,
            "axis": 90,
            "add_power": 1.5
        },
        {
            "product_name": "Progressive Lens",
            "sph": -15.0,
            "cyl": 0,
            "axis": 0,
            "add_power": 3.0
        }
    ]
}


# ----------------------------------
# RUN ENGINE
# ----------------------------------

engine = ValidationEngine()

results = engine.run(order_data)   # list of ValidationResult


# ----------------------------------
# ADAPT ENGINE → UI FORMAT
# ----------------------------------

final_result = {
    "order_id": order_data["order_id"],

    "is_valid": True,

    # UI expects these
    "errors": [],
    "warnings": [],

    # Extra info
    "details": []
}


for r in results:

    final_result["details"].append({
        "rule": r.rule,
        "passed": r.passed,
        "severity": r.severity,
        "message": r.message
    })

    if not r.passed:

        if r.severity == "CRITICAL":
            final_result["is_valid"] = False
            final_result["errors"].append(r.message)

        elif r.severity == "WARNING":
            final_result["warnings"].append(r.message)


# ----------------------------------
# DISPLAY
# ----------------------------------

show_result(final_result)


# ----------------------------------
# DECISION
# ----------------------------------

decision = suggest(final_result)

print(f"\n📌 System Suggestion: {decision}")


# ----------------------------------
# ACTION FLOW
# ----------------------------------

if decision == "APPROVE":

    print("\n✅ Order Approved Automatically")


elif decision == "REJECT":

    print("\n❌ Order Rejected")


elif decision == "REVIEW":

    print("\n⏳ Manager Review Needed")

    try:

        override = collect_override()

        print("\n✅ Override Accepted")
        print("Details:", override)

    except Exception as e:

        print("\n⚠️ Override Failed:", e)

        add(order_data["order_id"], final_result)


# ----------------------------------
# SHOW QUEUE
# ----------------------------------

show()
