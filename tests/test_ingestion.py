"""Tests for partner-specific event ingestion."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.database import get_session
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.main import (
    app,
    get_downstream_delivery_queue,
    get_event_persister,
)
from trackrelay.models import Partner
from trackrelay.services import (
    DownstreamDeliveryJob,
    DownstreamDeliveryQueue,
    DownstreamDeliveryQueueError,
    EventPersistenceResult,
    RecordingDownstreamDeliveryQueue,
)


class StubSession:
    """Return one configured partner without requiring a database."""

    def __init__(self, partner: Partner | None) -> None:
        self.partner = partner
        self.closed = False

    def get(self, model: type[Partner], partner_id: str) -> Partner | None:
        assert model is Partner
        if self.partner is not None and self.partner.id == partner_id:
            return self.partner
        return None

    def close(self) -> None:
        self.closed = True


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
    delivery_queue: DownstreamDeliveryQueue | None = None,
) -> StubSession:
    session = StubSession(partner)

    def override_session() -> Iterator[StubSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_event_persister] = lambda: persist_event
    app.dependency_overrides[get_downstream_delivery_queue] = lambda: (
        delivery_queue or RecordingDownstreamDeliveryQueue()
    )
    return session


def configured_partner(**overrides: Any) -> Partner:
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
    delivery_queue = RecordingDownstreamDeliveryQueue()
    session: StubSession

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        assert session.closed is True
        persisted.append(event)
        return EventPersistenceResult(
            event_id=persisted_event_id,
            duplicate=False,
        )

    session = configure_dependencies(
        configured_partner(),
        persist_event,
        delivery_queue,
    )

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 201
    assert response.json() == {
        "event_id": str(persisted_event_id),
        "processing_status": "processed",
        "duplicate": False,
        "delivery_status": "queued",
        "downstream_status_code": None,
    }
    assert len(persisted) == 1
    assert persisted[0].partner_id == "courier-alpha"
    assert persisted[0].partner_event_id == "ALPHA-001"
    assert persisted[0].tracking_number == "TRK-001"
    assert persisted[0].status is ShipmentStatus.PICKED_UP
    assert persisted[0].raw_payload == valid_payload
    assert delivery_queue.enqueued_jobs == (
        DownstreamDeliveryJob(event_id=persisted_event_id),
    )


def test_ingestion_attaches_test_run_metadata_outside_the_partner_payload(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted_event_id = uuid4()
    test_run_id = uuid4()
    persisted: list[NormalizedEvent] = []
    delivery_queue = RecordingDownstreamDeliveryQueue()

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(
            event_id=persisted_event_id,
            duplicate=False,
        )

    configure_dependencies(configured_partner(), persist_event, delivery_queue)

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        headers={"X-Test-Run-ID": str(test_run_id)},
        json=valid_payload,
    )

    assert response.status_code == 201
    assert len(persisted) == 1
    assert persisted[0].test_run_id == test_run_id
    assert persisted[0].raw_payload == valid_payload
    assert delivery_queue.enqueued_jobs == (
        DownstreamDeliveryJob(event_id=persisted_event_id),
    )


def test_ingestion_skips_delivery_and_returns_the_original_duplicate(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    original_event_id = uuid4()
    delivery_queue = RecordingDownstreamDeliveryQueue()

    def persist_duplicate(event: NormalizedEvent) -> EventPersistenceResult:
        return EventPersistenceResult(
            event_id=original_event_id,
            duplicate=True,
        )

    configure_dependencies(
        configured_partner(),
        persist_duplicate,
        delivery_queue,
    )

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
    assert delivery_queue.enqueued_jobs == ()


def test_ingestion_separates_partner_identity_from_beta_adapter_type(
    client: TestClient,
) -> None:
    persisted_event_id = uuid4()
    persisted: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(
            event_id=persisted_event_id,
            duplicate=False,
        )

    configure_dependencies(
        configured_partner(
            id="beta-indonesia",
            name="Beta Indonesia",
            adapter_type="courier-beta",
        ),
        persist_event,
    )

    beta_payload = {
        "messageId": "beta-7741",
        "awb": "BET987654321",
        "statusCode": 72,
        "timestamp": 1786000860,
    }
    response = client.post(
        "/api/v1/partners/beta-indonesia/events",
        json=beta_payload,
    )

    assert response.status_code == 201
    assert response.json()["event_id"] == str(persisted_event_id)
    assert len(persisted) == 1
    assert persisted[0].partner_id == "beta-indonesia"
    assert persisted[0].partner_event_id == "beta-7741"
    assert persisted[0].tracking_number == "BET987654321"
    assert persisted[0].status is ShipmentStatus.DELIVERED
    assert persisted[0].occurred_at == datetime(2026, 8, 6, 7, 21, tzinfo=UTC)
    assert persisted[0].raw_payload == beta_payload


def test_ingestion_selects_gamma_for_a_nested_utc_payload(
    client: TestClient,
) -> None:
    persisted_event_id = uuid4()
    persisted: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(
            event_id=persisted_event_id,
            duplicate=False,
        )

    configure_dependencies(
        configured_partner(
            id="gamma-indonesia",
            name="Gamma Indonesia",
            adapter_type="courier-gamma",
        ),
        persist_event,
    )

    gamma_payload = {
        "notification": {
            "reference": "gamma-9012",
            "trackingNumber": "GAM246813579",
            "status": "DELIVERED",
            "occurredAt": "2026-08-06T07:21:00Z",
        }
    }
    response = client.post(
        "/api/v1/partners/gamma-indonesia/events",
        json=gamma_payload,
    )

    assert response.status_code == 201
    assert response.json()["event_id"] == str(persisted_event_id)
    assert len(persisted) == 1
    assert persisted[0].partner_id == "gamma-indonesia"
    assert persisted[0].partner_event_id == "gamma-9012"
    assert persisted[0].tracking_number == "GAM246813579"
    assert persisted[0].status is ShipmentStatus.DELIVERED
    assert persisted[0].occurred_at == datetime(2026, 8, 6, 7, 21, tzinfo=UTC)
    assert persisted[0].raw_payload == gamma_payload


def test_ingestion_reports_queue_failure_after_persistence(
    client: TestClient,
    valid_payload: dict[str, str],
) -> None:
    persisted: list[NormalizedEvent] = []

    def persist_event(event: NormalizedEvent) -> EventPersistenceResult:
        persisted.append(event)
        return EventPersistenceResult(event_id=uuid4(), duplicate=False)

    class FailingQueue:
        def enqueue(self, job: DownstreamDeliveryJob) -> None:
            raise DownstreamDeliveryQueueError("simulated publish failure")

    configure_dependencies(configured_partner(), persist_event, FailingQueue())

    response = client.post(
        "/api/v1/partners/courier-alpha/events",
        json=valid_payload,
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Event persisted, but downstream delivery was not queued"
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
        configured_partner(is_active=False),
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
        configured_partner(adapter_type="unknown-adapter"),
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
        configured_partner(),
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
