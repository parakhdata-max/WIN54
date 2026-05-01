"""
Common formatting utilities
"""

def format_currency(amount):

    return f"₹{amount:,.2f}"


def format_status(status):

    icons = {
        "APPROVED": "✅",
        "REJECTED": "❌",
        "PENDING": "⏳"
    }

    return f"{icons.get(status,'')} {status}"
