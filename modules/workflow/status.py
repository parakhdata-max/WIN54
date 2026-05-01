# modules/workflow/status.py

from enum import Enum


class OrderStatus(Enum):

    # Creation
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"

    # Processing
    PACKED = "PACKED"
    SHIPPED = "SHIPPED"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"

    # Completion
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    # Returns
    RETURN_REQUESTED = "RETURN_REQUESTED"
    RETURNED = "RETURNED"

    # Refunds
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUNDED = "REFUNDED"
