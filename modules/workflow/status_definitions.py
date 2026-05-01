# modules/workflow/status_definitions.py


# -------------------------
# ORDER LEVEL STATUS
# -------------------------

ORDER_STATUS = [
    "DRAFT",
    "PROVISIONAL",
    "CONFIRMED",
    "CANCELLED"
]


VALIDATION_STATUS = [
    "PENDING",
    "VALIDATED",
    "FAILED",
    "OVERRIDDEN"
]


PRICING_STATUS = [
    "PENDING",
    "PRICED",
    "APPROVED"
]


FINANCIAL_STATUS = [
    "INVOICE_PENDING",
    "INVOICED",
    "PART_PAID",
    "PAID",
    "CREDIT_HOLD"
]


# -------------------------
# LINE LEVEL STATUS
# -------------------------

STOCK_FLOW = [
    "ALLOCATED",
    "PICKED",
    "BILLED",
    "DISPATCHED",
    "DELIVERED",
    "CLOSED"
]


INHOUSE_FLOW = [
    "JOB_CREATED",
    "MATERIAL_ISSUED",
    "IN_PRODUCTION",
    "FINISHING",
    "QC",
    "READY",
    "DISPATCHED",
    "CLOSED"
]


EXTERNAL_FLOW = [
    "LAB_ORDERED",
    "ACKNOWLEDGED",
    "IN_PROCESS",
    "RECEIVED",
    "QC",
    "READY",
    "DISPATCHED",
    "CLOSED"
]
