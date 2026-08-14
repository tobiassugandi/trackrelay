"""Tests for the Courier Alpha ingestion endpoint."""

from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.database import get_session
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.main import app, get_event_deliverer, get_event_persister
from trackrelay.models import Partner
from trackrelay.services import DeliveryResult, EventPersistenceResult


class StubSession:
    """Return one configured partner without requiring a database."""

    def __init__(self, partner: Partner | None) -> None:
        self.partner = partner

    def get(self, model: type[Partner], partner_id: str) -> Partner | None:
        assert model is Partner
        if self.partner is not None and self.partner.id == partner_id:
            return self.partner
        return None


@fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@fixture
def valid_payload() -> dict[str, str]:
    return {
        "event_id": "ALPHA-001",
        "tracking_number": "TRK-001",
        "status": "PICKUP",
        "event_time": "2026-08-06T10:00:00+07:00",
    }


def configure_dependencies(
    partner: Partner | None,
    persist_event: Callable[[NormalizedEvent], EventPersistenceResult],
    deliver_event: Callable[[NormalizedEvent, UUID], DeliveryResult] | None = None,
) -> None:
    def override_session() -> Iterator[StubSession]:
        yield StubSession(partner)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_event_persister] = lambda: persist_event
    app.dependency_overrides[get_event_deliverer] = lambda: (
        deliver_event
        or (lambda event, event_id: DeliveryResult(downstream_status_code=202))
    )


def alpha_partner(**overrides: Any) -> Partner:
    values = {
        "id": "courier-alpha",
        "name": "Courier Alpha",
        "adapter_type": "courier-alpha",
        "is_active": True,
    }
    values.update(overrides)
    return Partner(**values)


def test_ingestion_normalizes_and_persists_an_alpha_event(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted_event_id = uuid4()
    persisted: list[NormalizedEvent] = []
    delivered: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(
            event_id=persisted_event_id,
            duplicate=False,
        )

    def deliver_event(event: NormalizedEvent, event_id: UUID) -> DeliveryResult:
        delivered.append(event)
        return DeliveryResult(downstream_status_code=202)

    configure_dependencies(alpha_partner(), persist_event, deliver_event)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 201
    assert response.json() == {
        "event_id": str(persisted_event_id),
        "processing_status": "processed",
        "duplicate": False,
        "delivery_status": "delivered",
        "downstream_status_code": 202,
    }
    assert len(persisted) == 1
    assert persisted[0].partner_id == "courier-alpha"
    assert persisted[0].partner_event_id == "ALPHA-001"
    assert persisted[0].tracking_number == "TRK-001"
    assert persisted[0].status is ShipmentStatus.PICKED_UP
    assert persisted[0].raw_payload == valid_payload
    assert delivered == persisted


def test_ingestion_skips_delivery_and_returns_the_original_duplicate(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    original_event_id = uuid4()
    delivered: list[NormalizedEvent] = []

    def persist_duplicate(event: NormalizedEvent) -> EventPersistenceResult:
        return EventPersistenceResult(
            event_id=original_event_id,
            duplicate=True,
        )

    def deliver_event(event: NormalizedEvent, event_id: UUID) -> DeliveryResult:
        delivered.append(event)
        return DeliveryResult(downstream_status_code=202)

    configure_dependencies(alpha_partner(), persist_duplicate, deliver_event)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": str(original_event_id),
        "processing_status": "processed",
        "duplicate": True,
        "delivery_status": "skipped_duplicate",
        "downstream_status_code": None,
    }
    assert delivered == []


def test_ingestion_reports_a_downstream_server_error_after_persistence(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(event_id=uuid4(), duplicate=False)

    def fail_delivery(event: NormalizedEvent, event_id: UUID) -> DeliveryResult:
        request = httpx.Request("POST", "http://downstream.test/events")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError(
            "simulated downstream failure",
            request=request,
            response=response,
        )

    configure_dependencies(alpha_partner(), persist_event, fail_delivery)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Downstream delivery failed; event remains persisted"
    }
    assert len(persisted) == 1


def test_ingestion_reports_a_downstream_timeout_after_persistence(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(event_id=uuid4(), duplicate=False)

    def time_out(event: NormalizedEvent, event_id: UUID) -> DeliveryResult:
        request = httpx.Request("POST", "http://downstream.test/events")
        raise httpx.ReadTimeout("simulated timeout", request=request)

    configure_dependencies(alpha_partner(), persist_event, time_out)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 504
    assert response.json() == {
        "detail": "Downstream delivery timed out; event remains persisted"
    }
    assert len(persisted) == 1


def test_ingestion_rejects_an_unknown_partner(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted: list[NormalizedEvent] = []
    configure_dependencies(
        None,
        lambda event: EventPersistenceResult(
            event_id=uuid4(),
            duplicate=False,
        ),
    )

    response = client.post(
        "/api/v1/partners/unknown/events",
        json=valid_payload,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Partner not found"}
    assert persisted == []


def test_ingestion_rejects_an_inactive_partner(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    configure_dependencies(
        alpha_partner(is_active=False),
        lambda event: EventPersistenceResult(
            event_id=uuid4(),
            duplicate=False,
        ),
    )

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Partner is inactive"}


def test_ingestion_rejects_an_unsupported_partner_adapter(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    configure_dependencies(
        alpha_partner(adapter_type="courier-beta"),
        lambda event: EventPersistenceResult(
            event_id=uuid4(),
            duplicate=False,
        ),
    )

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Partner adapter is not supported"}


def test_ingestion_rejects_an_invalid_alpha_payload(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    configure_dependencies(
        alpha_partner(),
        lambda event: EventPersistenceResult(
            event_id=uuid4(),
            duplicate=False,
        ),
    )
    valid_payload["status"] = "UNKNOWN"

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 422
