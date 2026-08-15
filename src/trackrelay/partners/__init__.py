"""Courier partner contracts and adapters."""

from trackrelay.partners.alpha import (
    AlphaStatusCode,
    CourierAlphaAdapter,
    CourierAlphaPayload,
)
from trackrelay.partners.base import PartnerAdapter

__all__ = [
    "AlphaStatusCode",
    "CourierAlphaAdapter",
    "CourierAlphaPayload",
    "PartnerAdapter",
]
