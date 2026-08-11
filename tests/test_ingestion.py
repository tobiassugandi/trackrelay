"""Tests for the Courier Alpha ingestion endpoint."""

from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.database import get_session
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.main import app, get_event_persister
from trackrelay.models import Partner


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
    persist_event: Callable[[NormalizedEvent], UUID],
) -> None:
    def override_session() -> Iterator[StubSession]:
        yield StubSession(partner)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_event_persister] = lambda: persist_event


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

    def persist_event(event: NormalizedEvent) -> UUID:
        persisted.append(event)
        return persisted_event_id

    configure_dependencies(alpha_partner(), persist_event)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 201
    assert response.json() == {
        "event_id": str(persisted_event_id),
        "status": "processed",
    }
    assert len(persisted) == 1
    assert persisted[0].partner_id == "courier-alpha"
    assert persisted[0].partner_event_id == "ALPHA-001"
    assert persisted[0].tracking_number == "TRK-001"
    assert persisted[0].status is ShipmentStatus.PICKED_UP
    assert persisted[0].raw_payload == valid_payload


def test_ingestion_rejects_an_unknown_partner(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted: list[NormalizedEvent] = []
    configure_dependencies(None, lambda event: persisted.append(event) or uuid4())

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
    configure_dependencies(alpha_partner(is_active=False), lambda event: uuid4())

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
        lambda event: uuid4(),
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
    configure_dependencies(alpha_partner(), lambda event: uuid4())
    valid_payload["status"] = "UNKNOWN"

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 422
