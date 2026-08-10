"""Normalized event domain schema."""

from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    StringConstraints,
)

from trackrelay.domain.shipment import ShipmentStatus

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class NormalizedEvent(BaseModel):
    """A courier event translated into TrackRelay's internal vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    partner_id: Identifier
    partner_event_id: Identifier
    tracking_number: Identifier
    status: ShipmentStatus
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    raw_payload: dict[str, JsonValue]
