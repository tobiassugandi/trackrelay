"""Courier Alpha's external payload contract."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from trackrelay.domain import NormalizedEvent, ShipmentStatus

AlphaIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AlphaStatusCode(StrEnum):
    """Status codes defined by Courier Alpha, not by TrackRelay."""

    CREATED = "CREATED"
    PICKED_UP = "PICKUP"
    IN_TRANSIT = "TRANSIT"
    OUT_FOR_DELIVERY = "OFD"
    DELIVERED = "POD"


class CourierAlphaPayload(BaseModel):
    """The JSON body Courier Alpha sends to TrackRelay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: AlphaIdentifier
    tracking_number: AlphaIdentifier
    status: AlphaStatusCode
    event_time: AwareDatetime


ALPHA_STATUS_MAP: dict[AlphaStatusCode, ShipmentStatus] = {
    AlphaStatusCode.CREATED: ShipmentStatus.CREATED,
    AlphaStatusCode.PICKED_UP: ShipmentStatus.PICKED_UP,
    AlphaStatusCode.IN_TRANSIT: ShipmentStatus.IN_TRANSIT,
    AlphaStatusCode.OUT_FOR_DELIVERY: ShipmentStatus.OUT_FOR_DELIVERY,
    AlphaStatusCode.DELIVERED: ShipmentStatus.DELIVERED,
}


class CourierAlphaAdapter:
    """Translate Courier Alpha's contract into TrackRelay's domain contract."""

    partner_id = "courier-alpha"
    payload_model = CourierAlphaPayload

    def normalize(
        self,
        payload: CourierAlphaPayload,
        *,
        received_at: datetime,
    ) -> NormalizedEvent:
        """Return one normalized event without performing I/O."""
        return NormalizedEvent(
            partner_id=self.partner_id,
            partner_event_id=payload.event_id,
            tracking_number=payload.tracking_number,
            status=ALPHA_STATUS_MAP[payload.status],
            occurred_at=payload.event_time,
            received_at=received_at,
            raw_payload=payload.model_dump(mode="json", by_alias=True),
        )
