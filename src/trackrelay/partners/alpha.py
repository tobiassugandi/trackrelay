"""Courier Alpha's external payload contract."""

from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

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
