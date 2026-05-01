"""
Display validation results
"""

from .formatter import format_currency, format_status


def show_result(result):

    status = "APPROVED" if result["is_valid"] else "REJECTED"

    print("\n" + "=" * 50)
    print(format_status(status))
    print("=" * 50)

    if result["errors"]:

        print("\n❌ ERRORS:")

        for e in result["errors"]:
            print(f" - {e['message']}")

    if result["warnings"]:

        print("\n⚠️ WARNINGS:")

        for w in result["warnings"]:
            print(f" - {w['message']}")

    print("=" * 50)
