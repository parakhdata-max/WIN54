"""
modules/validators/registry.py
================================
Maps rule names from validation_rules.json to validator classes.
"""
from .order_validator        import OrderValidator
from .fulfillment_validator  import FulfillmentValidator
from .party_validator     import PartyValidator
from .product_validator   import ProductValidator
from .rx_validator        import RxValidator
from .financial_validator import FinancialValidator
from .tax_validator       import TaxValidator

RULE_REGISTRY = {
    "ORDER":     OrderValidator,
    "PARTY":     PartyValidator,
    "PRODUCT":   ProductValidator,
    "RX":        RxValidator,
    "FINANCIAL": FinancialValidator,
    "TAX":         TaxValidator,
    "FULFILLMENT": FulfillmentValidator,
}

RULE_DESCRIPTIONS = {
    "ORDER":     "Order structure, required fields, line qty, batch allocation integrity",
    "PARTY":     "Party name present, not in blocklist",
    "PRODUCT":   "No discontinued products in order lines",
    "RX":        "SPH/CYL/AXIS/ADD within optical range",
    "FINANCIAL": "Credit limit, price non-zero, total matches unit×qty",
    "TAX":         "gst_percent present, valid Indian slab (0/5/12/18/28%)",
    "FULFILLMENT": "Supplier assigned, billing gate (purchase required), auto-fulfillment power match",
}
