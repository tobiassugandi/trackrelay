"""Tests for event-level processing and delivery diagnostics."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.database import (
    Base,
    create_database_engine,
    create_session_factory,
    get_session,
)
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    ShipmentStatus,
)
from trackrelay.main import app
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment

EVENT_TIME = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)


@fixture
def event_client(tmp_path: Path) -> Iterator[tuple[TestClient, UUID]]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'event.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with sessions.begin() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            Shipment(
                tracking_number="EVENT-TRK-001",
                current_status=ShipmentStatus.PICKED_UP,
                current_status_occurred_at=EVENT_TIME,
            )
        )
        session.flush()
        event = Event(
            partner_id="courier-alpha",
            partner_event_id="EVENT-INSPECTION-001",
            tracking_number="EVENT-TRK-001",
            status=ShipmentStatus.PICKED_UP,
            occurred_at=EVENT_TIME,
            received_at=EVENT_TIME + timedelta(seconds=2),
            raw_payload={"status": "PICKUP"},
            processing_status=EventProcessingStatus.PROCESSED,
            state_applied=True,
        )
        session.add(event)
        session.flush()
        event_id = event.id
        session.add_all(
            [
                DeliveryAttempt(
                    event_id=event_id,
                    attempt_number=2,
                    result=DeliveryAttemptResult.DELIVERED,
                    response_code=202,
                    latency_ms=40,
                    error=None,
                    started_at=EVENT_TIME + timedelta(seconds=4),
                    completed_at=EVENT_TIME + timedelta(seconds=4, milliseconds=40),
                ),
                DeliveryAttempt(
                    event_id=event_id,
                    attempt_number=1,
                    result=DeliveryAttemptResult.HTTP_ERROR,
                    response_code=503,
                    latency_ms=25,
                    error="simulated unavailability",
                    started_at=EVENT_TIME + timedelta(seconds=3),
                    completed_at=EVENT_TIME + timedelta(seconds=3, milliseconds=25),
                ),
            ]
        )

    def override_session() -> Iterator[object]:
        with sessions() as session:
            yield session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client, event_id
    app.dependency_overrides.clear()
    engine.dispose()


def test_get_event_returns_processing_and_ordered_delivery_diagnostics(
    event_client: tuple[TestClient, UUID],
) -> None:
    client, event_id = event_client

    response = client.get(f"/api/v1/events/{event_id}")

    assert response.status_code == 200
    event = response.json()
    assert event["id"] == str(event_id)
    assert event["partner_event_id"] == "EVENT-INSPECTION-001"
    assert event["tracking_number"] == "EVENT-TRK-001"
    assert event["status"] == "picked_up"
    assert event["processing_status"] == "processed"
    assert event["state_applied"] is True
    assert event["state_rejection_reason"] is None
    assert event["raw_payload"] == {"status": "PICKUP"}

    attempts = event["delivery_attempts"]
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["result"] for attempt in attempts] == [
        "http_error",
        "delivered",
    ]
    assert [attempt["response_code"] for attempt in attempts] == [503, 202]
    assert [attempt["latency_ms"] for attempt in attempts] == [25, 40]
    assert [attempt["error"] for attempt in attempts] == [
        "simulated unavailability",
        None,
    ]
    assert all(attempt["started_at"] for attempt in attempts)
    assert all(attempt["completed_at"] for attempt in attempts)


def test_get_event_returns_not_found_for_an_unknown_id(
    event_client: tuple[TestClient, UUID],
) -> None:
    client, _ = event_client

    response = client.get(f"/api/v1/events/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
