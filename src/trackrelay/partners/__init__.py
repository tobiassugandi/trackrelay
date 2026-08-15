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
from trackrelay.partners.gamma import (
    CourierGammaAdapter,
    CourierGammaPayload,
    GammaNotification,
    GammaStatusCode,
)

PARTNER_ADAPTERS: dict[str, PartnerAdapter[BaseModel]] = {
    adapter.adapter_type: adapter
    for adapter in (
        CourierAlphaAdapter(),
        CourierBetaAdapter(),
        CourierGammaAdapter(),
    )
}

__all__ = [
    "PARTNER_ADAPTERS",
    "AlphaStatusCode",
    "BetaStatusCode",
    "CourierAlphaAdapter",
    "CourierAlphaPayload",
    "CourierBetaAdapter",
    "CourierBetaPayload",
    "CourierGammaAdapter",
    "CourierGammaPayload",
    "GammaNotification",
    "GammaStatusCode",
    "PartnerAdapter",
]
