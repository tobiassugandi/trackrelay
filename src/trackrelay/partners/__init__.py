"""Courier partner contracts and adapters."""

from pydantic import BaseModel

from trackrelay.partners.alpha import (
    AlphaStatusCode,
    CourierAlphaAdapter,
    CourierAlphaPayload,
)
from trackrelay.partners.base import PartnerAdapter
from trackrelay.partners.beta import (
    BetaStatusCode,
    CourierBetaAdapter,
    CourierBetaPayload,
)

PARTNER_ADAPTERS: dict[str, PartnerAdapter[BaseModel]] = {
    adapter.partner_id: adapter
    for adapter in (CourierAlphaAdapter(), CourierBetaAdapter())
}

__all__ = [
    "PARTNER_ADAPTERS",
    "AlphaStatusCode",
    "BetaStatusCode",
    "CourierAlphaAdapter",
    "CourierAlphaPayload",
    "CourierBetaAdapter",
    "CourierBetaPayload",
    "PartnerAdapter",
]
