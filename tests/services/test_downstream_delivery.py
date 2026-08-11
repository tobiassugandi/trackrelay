"""Tests for synchronous normalized-event delivery."""

import json
from datetime import UTC, datetime

import httpx
from pytest import raises

from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.services import deliver_normalized_event


def normalized_event() -> NormalizedEvent:
    return NormalizedEvent(
        partner_id="courier-alpha",
        partner_event_id="ALPHA-001",
        tracking_number="TRK-001",
        status=ShipmentStatus.PICKED_UP,
        occurred_at=datetime(2026, 8, 6, 3, 0, tzinfo=UTC),
        received_at=datetime(2026, 8, 6, 3, 0, 2, tzinfo=UTC),
        raw_payload={"status": "PICKUP"},
    )


def test_delivery_posts_the_normalized_event_and_records_success() -> None:
    def accept(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://downstream.test/events"
        assert json.loads(request.content) == normalized_event().model_dump(mode="json")
        return httpx.Response(202, json={"status": "accepted"})

    with httpx.Client(transport=httpx.MockTransport(accept)) as client:
        result = deliver_normalized_event(
            normalized_event(),
            downstream_url="http://downstream.test/",
            client=client,
        )

    assert result.status == "delivered"
    assert result.downstream_status_code == 202


def test_delivery_raises_for_a_downstream_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with httpx.Client(transport=transport) as client, raises(httpx.HTTPStatusError):
        deliver_normalized_event(
            normalized_event(),
            downstream_url="http://downstream.test",
            client=client,
        )
