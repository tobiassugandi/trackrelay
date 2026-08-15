"""Shared contract implemented by every courier adapter."""

from datetime import datetime
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from trackrelay.domain import NormalizedEvent

PayloadT = TypeVar("PayloadT", bound=BaseModel)


@runtime_checkable
class PartnerAdapter(Protocol[PayloadT]):
    """Validate one courier contract and normalize it without performing I/O."""

    adapter_type: str
    payload_model: type[PayloadT]

    def normalize(
        self,
        payload: PayloadT,
        *,
        partner_id: str,
        received_at: datetime,
    ) -> NormalizedEvent:
        """Translate one validated courier payload into the shared domain."""
        ...
