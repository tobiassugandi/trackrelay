"""Courier Gamma's nested external payload contract."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic_core import PydanticCustomError

from trackrelay.domain import NormalizedEvent, ShipmentStatus

GammaIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class GammaStatusCode(StrEnum):
    """Status codes nested inside Courier Gamma notifications."""

    CREATED = "CREATED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"


class GammaNotification(BaseModel):
    """The nested notification object sent by Courier Gamma."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: GammaIdentifier
    tracking_number: GammaIdentifier = Field(alias="trackingNumber")
    status: GammaStatusCode
    occurred_at: AwareDatetime = Field(alias="occurredAt")

    @field_validator("occurred_at", mode="before")
    @classmethod
    def require_iso_timestamp(cls, value: object) -> object:
        """Reject numeric timestamps instead of letting Pydantic coerce them."""
        if not isinstance(value, str):
            raise PydanticCustomError(
                "gamma_iso_timestamp",
                "occurredAt must be an ISO timestamp string",
            )
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Accept aware timestamps only when their UTC offset is zero."""
        if value.utcoffset() != timedelta(0):
            raise ValueError("occurredAt must be an ISO UTC timestamp")
        return value


class CourierGammaPayload(BaseModel):
    """The top-level JSON body Courier Gamma sends to TrackRelay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notification: GammaNotification


GAMMA_STATUS_MAP: dict[GammaStatusCode, ShipmentStatus] = {
    GammaStatusCode.CREATED: ShipmentStatus.CREATED,
    GammaStatusCode.PICKED_UP: ShipmentStatus.PICKED_UP,
    GammaStatusCode.IN_TRANSIT: ShipmentStatus.IN_TRANSIT,
    GammaStatusCode.OUT_FOR_DELIVERY: ShipmentStatus.OUT_FOR_DELIVERY,
    GammaStatusCode.DELIVERED: ShipmentStatus.DELIVERED,
}


class CourierGammaAdapter:
    """Translate Courier Gamma's contract into TrackRelay's domain contract."""

    adapter_type = "courier-gamma"
    payload_model = CourierGammaPayload

    def normalize(
        self,
        payload: CourierGammaPayload,
        *,
        partner_id: str,
        received_at: datetime,
    ) -> NormalizedEvent:
        """Return one normalized event without performing I/O."""
        notification = payload.notification
        return NormalizedEvent(
            partner_id=partner_id,
            partner_event_id=notification.reference,
            tracking_number=notification.tracking_number,
            status=GAMMA_STATUS_MAP[notification.status],
            occurred_at=notification.occurred_at,
            received_at=received_at,
            raw_payload=payload.model_dump(mode="json", by_alias=True),
        )
