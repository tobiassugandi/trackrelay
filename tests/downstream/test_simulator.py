"""Tests for the downstream simulator API."""

from collections.abc import Iterator

from fastapi.testclient import TestClient
from pytest import fixture

from trackrelay.downstream.main import app, event_store, simulator_control


@fixture
def client() -> Iterator[TestClient]:
    event_store.clear()
    simulator_control.reset()
    with TestClient(app) as test_client:
        yield test_client
    event_store.clear()
    simulator_control.reset()


def normalized_event_data() -> dict[str, object]:
    return {
        "partner_id": "courier-alpha",
        "partner_event_id": "ALPHA-001",
        "tracking_number": "TRK-001",
        "status": "picked_up",
        "occurred_at": "2026-08-06T10:00:00+07:00",
        "received_at": "2026-08-06T10:00:02+07:00",
        "raw_payload": {"status": "PICKUP"},
    }


def test_simulator_accepts_and_exposes_a_normalized_event(
    client: TestClient,
) -> None:
    response = client.post("/events", json=normalized_event_data())

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "received_count": 1}

    stored_events = client.get("/events")
    assert stored_events.status_code == 200
    assert stored_events.json() == [normalized_event_data()]


def test_simulator_rejects_an_invalid_event_without_recording_it(
    client: TestClient,
) -> None:
    invalid_event = normalized_event_data()
    del invalid_event["received_at"]

    response = client.post("/events", json=invalid_event)

    assert response.status_code == 422
    assert client.get("/events").json() == []


def test_control_status_starts_healthy(client: TestClient) -> None:
    response = client.get("/control/status")

    assert response.status_code == 200
    assert response.json() == {"mode": "HEALTHY"}


def test_return_500_mode_rejects_without_recording_and_is_resettable(
    client: TestClient,
) -> None:
    mode_response = client.put(
        "/control/mode",
        json={"mode": "RETURN_500"},
    )

    assert mode_response.status_code == 200
    assert mode_response.json() == {"mode": "RETURN_500"}
    assert client.get("/control/status").json() == {"mode": "RETURN_500"}

    failed_delivery = client.post("/events", json=normalized_event_data())

    assert failed_delivery.status_code == 500
    assert failed_delivery.json() == {"detail": "Simulated downstream failure"}
    assert client.get("/events").json() == []

    client.put("/control/mode", json={"mode": "HEALTHY"})
    successful_delivery = client.post("/events", json=normalized_event_data())

    assert successful_delivery.status_code == 202
    assert client.get("/events").json() == [normalized_event_data()]


def test_control_rejects_an_unknown_mode(client: TestClient) -> None:
    response = client.put("/control/mode", json={"mode": "UNKNOWN"})

    assert response.status_code == 422
    assert client.get("/control/status").json() == {"mode": "HEALTHY"}
