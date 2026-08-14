"""Tests for shipment event history inspection."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.database import (
    Base,
    create_database_engine,
    create_session_factory,
    get_session,
)
from trackrelay.domain import EventProcessingStatus, ShipmentStatus
from trackrelay.main import app
from trackrelay.models import Event, Partner, Shipment

TRACKING_NUMBER = "HISTORY-TRK-001"
BASE_TIME = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


@fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'history.db'}")
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
                tracking_number=TRACKING_NUMBER,
                current_status=ShipmentStatus.DELIVERED,
                current_status_occurred_at=BASE_TIME + timedelta(minutes=3),
            )
        )
        session.flush()
        session.add_all(
            [
                Event(
                    partner_id="courier-alpha",
                    partner_event_id="DELIVERED-001",
                    tracking_number=TRACKING_NUMBER,
                    status=ShipmentStatus.DELIVERED,
                    occurred_at=BASE_TIME + timedelta(minutes=3),
                    received_at=BASE_TIME + timedelta(minutes=3, seconds=2),
                    raw_payload={"status": "POD"},
                    processing_status=EventProcessingStatus.PROCESSED,
                    state_applied=True,
                ),
                Event(
                    partner_id="courier-alpha",
                    partner_event_id="STALE-001",
                    tracking_number=TRACKING_NUMBER,
                    status=ShipmentStatus.OUT_FOR_DELIVERY,
                    occurred_at=BASE_TIME + timedelta(minutes=1),
                    received_at=BASE_TIME + timedelta(minutes=4),
                    raw_payload={"status": "OFD"},
                    processing_status=EventProcessingStatus.PROCESSED,
                    state_applied=False,
                    state_rejection_reason="stale_event",
                ),
            ]
        )

    def override_session() -> Iterator[object]:
        with sessions() as session:
            yield session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


def test_history_returns_applied_and_rejected_events_by_business_time(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/shipments/{TRACKING_NUMBER}/events")

    assert response.status_code == 200
    events = response.json()
    assert [event["partner_event_id"] for event in events] == [
        "STALE-001",
        "DELIVERED-001",
    ]
    assert events[0]["state_applied"] is False
    assert events[0]["state_rejection_reason"] == "stale_event"
    assert events[1]["state_applied"] is True
    assert events[1]["state_rejection_reason"] is None


def test_history_returns_not_found_for_an_unknown_shipment(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/shipments/UNKNOWN/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "Shipment not found"}


def test_get_shipment_returns_its_latest_accepted_state(
    client: TestClient,
) -> None:
    response = client.get(f"/api/v1/shipments/{TRACKING_NUMBER}")

    assert response.status_code == 200
    shipment = response.json()
    assert set(shipment) == {
        "tracking_number",
        "current_status",
        "current_status_occurred_at",
        "created_at",
        "updated_at",
    }
    assert shipment["tracking_number"] == TRACKING_NUMBER
    assert shipment["current_status"] == "delivered"
    assert datetime.fromisoformat(shipment["current_status_occurred_at"]) == (
        BASE_TIME + timedelta(minutes=3)
    ).replace(tzinfo=None)
    created_at = datetime.fromisoformat(shipment["created_at"])
    updated_at = datetime.fromisoformat(shipment["updated_at"])
    assert created_at <= updated_at


def test_get_shipment_returns_not_found_for_an_unknown_tracking_number(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/shipments/UNKNOWN")

    assert response.status_code == 404
    assert response.json() == {"detail": "Shipment not found"}
