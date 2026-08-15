"""Courier Beta's external payload contract."""

from datetime import UTC, datetime
from enum import IntEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

from trackrelay.domain import NormalizedEvent, ShipmentStatus

BetaIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class BetaStatusCode(IntEnum):
    """Integer status codes defined by Courier Beta."""

    CREATED = 10
    PICKED_UP = 20
    IN_TRANSIT = 30
    OUT_FOR_DELIVERY = 60
    DELIVERED = 72


class CourierBetaPayload(BaseModel):
    """The JSON body Courier Beta sends to TrackRelay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: BetaIdentifier = Field(alias="messageId")
    awb: BetaIdentifier
    status_code: BetaStatusCode = Field(alias="statusCode")
    timestamp: StrictInt = Field(gt=0)

    @field_validator("status_code", mode="before")
    @classmethod
    def require_integer_status_code(cls, value: Any) -> Any:
        """Reject stringified codes instead of silently coercing them."""
        if type(value) is not int:
            raise ValueError("statusCode must be an integer")
        return value


BETA_STATUS_MAP: dict[BetaStatusCode, ShipmentStatus] = {
    BetaStatusCode.CREATED: ShipmentStatus.CREATED,
    BetaStatusCode.PICKED_UP: ShipmentStatus.PICKED_UP,
    BetaStatusCode.IN_TRANSIT: ShipmentStatus.IN_TRANSIT,
    BetaStatusCode.OUT_FOR_DELIVERY: ShipmentStatus.OUT_FOR_DELIVERY,
    BetaStatusCode.DELIVERED: ShipmentStatus.DELIVERED,
}


class CourierBetaAdapter:
    """Translate Courier Beta's contract into TrackRelay's domain contract."""

    partner_id = "courier-beta"
    payload_model = CourierBetaPayload

    def normalize(
        self,
        payload: CourierBetaPayload,
        *,
        received_at: datetime,
    ) -> NormalizedEvent:
        """Return one normalized event without performing I/O."""
        return NormalizedEvent(
            partner_id=self.partner_id,
            partner_event_id=payload.message_id,
            tracking_number=payload.awb,
            status=BETA_STATUS_MAP[payload.status_code],
            occurred_at=datetime.fromtimestamp(payload.timestamp, tz=UTC),
            received_at=received_at,
            raw_payload=payload.model_dump(mode="json", by_alias=True),
        )
