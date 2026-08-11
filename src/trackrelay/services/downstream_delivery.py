"""Synchronous delivery to the downstream order system."""

from dataclasses import dataclass
from typing import Literal

import httpx

from trackrelay.domain import NormalizedEvent


@dataclass(frozen=True)
class DeliveryResult:
    """The smallest useful record of one successful HTTP delivery."""

    downstream_status_code: int
    status: Literal["delivered"] = "delivered"


def deliver_normalized_event(
    normalized_event: NormalizedEvent,
    *,
    downstream_url: str,
    timeout_seconds: float = 5.0,
    client: httpx.Client | None = None,
) -> DeliveryResult:
    """POST one normalized event and return its successful delivery result."""
    endpoint = f"{downstream_url.rstrip('/')}/events"
    body = normalized_event.model_dump(mode="json")

    if client is not None:
        response = client.post(endpoint, json=body)
    else:
        with httpx.Client(timeout=timeout_seconds) as http_client:
            response = http_client.post(endpoint, json=body)

    response.raise_for_status()
    return DeliveryResult(downstream_status_code=response.status_code)
