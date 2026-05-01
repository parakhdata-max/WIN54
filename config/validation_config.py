"""
Validation Configuration for Order Processing
============================================

This file contains all business rules for order validation.
Edit this file to add/remove blocked parties, set thresholds, etc.

Last Updated: Jan 2026
"""

# ═══════════════════════════════════════════════════════════════
# BLOCKED ENTITIES (Auto-Reject)
# ═══════════════════════════════════════════════════════════════

BLOCKED_PARTIES = [
    # Add party names here to block them from placing orders
    # Example:
    # 'ABC Opticals (Bad Debtor)',
    # 'XYZ Company (Fraud)',
]

DISCONTINUED_PRODUCTS = [
    # Add product IDs here to prevent orders
    # Example:
    # 'PROD-001',
    # 'PROD-OLD-123',
]

# ═══════════════════════════════════════════════════════════════
# TRUSTED ENTITIES (Auto-Approve)
# ═══════════════════════════════════════════════════════════════

TRUSTED_PARTIES = [
    # Parties that get auto-approved (if stock available)
    # Example:
    # 'VIP Customer Ltd',
    # 'Long Term Partner',
]

# ═══════════════════════════════════════════════════════════════
# VALIDATION THRESHOLDS
# ═══════════════════════════════════════════════════════════════

THRESHOLDS = {
    # Auto-approve limits (below these = auto-approve if valid)
    'AUTO_APPROVE_RETAIL': 5000,      # ₹5,000
    'AUTO_APPROVE_WHOLESALE': 25000,   # ₹25,000
    
    # High value alert (above this = needs manager review)
    'HIGH_VALUE_ORDER': 100000,        # ₹1,00,000
    
    # Credit limit buffer
    'CREDIT_BUFFER_PERCENT': 90,       # Warn if using > 90% credit
}

# ═══════════════════════════════════════════════════════════════
# RX VALIDATION LIMITS
# ═══════════════════════════════════════════════════════════════

RX_LIMITS = {
    'SPH_MIN': -20.0,
    'SPH_MAX': 20.0,
    'CYL_MIN': -10.0,
    'CYL_MAX': 10.0,
    'AXIS_MIN': 0,
    'AXIS_MAX': 180,
    'ADD_MIN': 0.0,
    'ADD_MAX': 4.0,
}

# ═══════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ═══════════════════════════════════════════════════════════════

DUPLICATE_CHECK = {
    'ENABLED': False,  # Set to True to enable duplicate detection
    'TIME_WINDOW_MINUTES': 30,  # Check orders in last 30 minutes
}

# ═══════════════════════════════════════════════════════════════
# ENABLE / DISABLE VALIDATORS
# ═══════════════════════════════════════════════════════════════

ENABLED_RULES = {

    'FINANCIAL': True,
    'PARTY': True,
    'PRODUCT': True,
    'RX': True,
    'DUPLICATE': False,

}

# ═══════════════════════════════════════════════════════════════
# VALIDATION CONFIGURATION (Complete)
# ═══════════════════════════════════════════════════════════════

VALIDATION_CONFIG = {

    'enabled_rules': ENABLED_RULES,

    'BLOCKED_PARTIES': BLOCKED_PARTIES,
    'DISCONTINUED_PRODUCTS': DISCONTINUED_PRODUCTS,
    'TRUSTED_PARTIES': TRUSTED_PARTIES,
    'THRESHOLDS': THRESHOLDS,
    'RX_LIMITS': RX_LIMITS,
    'DUPLICATE_CHECK': DUPLICATE_CHECK,

    'financial': {
        'warning_ratio': THRESHOLDS['CREDIT_BUFFER_PERCENT'] / 100,
        'block_ratio': 1.0,
    },

}


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def is_party_blocked(party_name: str) -> bool:
    """Check if a party is blocked"""
    return party_name in VALIDATION_CONFIG['BLOCKED_PARTIES']


def is_product_discontinued(product_id: str) -> bool:
    """Check if a product is discontinued"""
    return product_id in DISCONTINUED_PRODUCTS


def is_party_trusted(party_name: str) -> bool:
    """Check if a party is trusted"""
    return party_name in TRUSTED_PARTIES


def get_auto_approve_limit(order_source: str) -> float:
    """Get auto-approve limit for order source"""
    if order_source == 'RETAIL':
        return THRESHOLDS['AUTO_APPROVE_RETAIL']
    elif order_source == 'WHOLESALE':
        return THRESHOLDS['AUTO_APPROVE_WHOLESALE']
    return 0  # No auto-approve for other sources


# ═══════════════════════════════════════════════════════════════
# EXPORTS
# ═══════════════════════════════════════════════════════════════

__all__ = [
    'VALIDATION_CONFIG',
    'is_party_blocked',
    'is_product_discontinued',
    'is_party_trusted',
    'get_auto_approve_limit',
]