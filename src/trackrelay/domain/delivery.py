"""Downstream delivery domain types."""

from enum import StrEnum


class DeliveryAttemptResult(StrEnum):
    """The observable outcomes of one downstream transport attempt."""

    DELIVERED = "delivered"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
