"""Tests for the downstream simulator API."""

from collections.abc import Iterator
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from pytest import fixture, mark

from trackrelay.downstream.control import SLOW_DELAY_SECONDS, TIMEOUT_DELAY_SECONDS
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


def test_liveness(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_runtime_metrics_describe_the_downstream_process_only(
    client: TestClient,
) -> None:
    response = client.get("/experiments/runtime-metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 2
    assert body["process_id"] > 0
    assert body["python_thread_count"] > 0
    assert body["database_pool"] is None


def test_control_event_count_does_not_transfer_receipt_payloads(
    client: TestClient,
) -> None:
    response = client.get("/control/events/count")

    assert response.status_code == 200
    assert response.json() == {"event_count": 0}


def test_simulator_accepts_and_exposes_a_normalized_event(
    client: TestClient,
) -> None:
    response = client.post("/events", json=normalized_event_data())

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "received_count": 1,
        "duplicate": False,
    }

    stored_events = client.get("/events")
    assert stored_events.status_code == 200
    assert stored_events.json() == [normalized_event_data()]


def test_simulator_accepts_an_idempotent_replay_without_recording_it_twice(
    client: TestClient,
) -> None:
    event_id = UUID("00000000-0000-0000-0000-000000000801")
    headers = {"Idempotency-Key": str(event_id)}

    first_response = client.post(
        "/events",
        json=normalized_event_data(),
        headers=headers,
    )
    replay_response = client.post(
        "/events",
        json=normalized_event_data(),
        headers=headers,
    )

    assert first_response.status_code == 202
    assert first_response.json() == {
        "status": "accepted",
        "received_count": 1,
        "duplicate": False,
    }
    assert replay_response.status_code == 202
    assert replay_response.json() == {
        "status": "accepted",
        "received_count": 1,
        "duplicate": True,
    }
    assert client.get("/events").json() == [normalized_event_data()]


def test_simulator_preserves_a_synthetic_events_test_run_id(
    client: TestClient,
) -> None:
    test_run_id = UUID("00000000-0000-0000-0000-000000000701")
    event = normalized_event_data()
    event["test_run_id"] = str(test_run_id)

    response = client.post("/events", json=event)

    assert response.status_code == 202
    assert client.get("/events").json()[0]["test_run_id"] == str(test_run_id)


def test_simulator_filters_receipts_by_test_run_id(client: TestClient) -> None:
    selected_run_id = UUID("00000000-0000-0000-0000-000000000701")
    other_run_id = UUID("00000000-0000-0000-0000-000000000702")
    for test_run_id in (selected_run_id, other_run_id):
        event = normalized_event_data()
        event["partner_event_id"] = str(test_run_id)
        event["test_run_id"] = str(test_run_id)
        assert client.post("/events", json=event).status_code == 202

    response = client.get(
        "/events",
        params={"test_run_id": str(selected_run_id)},
    )

    assert response.status_code == 200
    assert [event["test_run_id"] for event in response.json()] == [str(selected_run_id)]


def test_private_control_clears_and_counts_all_receipts(
    client: TestClient,
) -> None:
    assert client.post("/events", json=normalized_event_data()).status_code == 202

    response = client.delete("/control/events")

    assert response.status_code == 200
    assert response.json() == {"cleared_event_count": 1}
    assert client.get("/events").json() == []


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
    assert response.json() == {"mode": "HEALTHY", "delay_seconds": 0.0}


def test_return_500_mode_rejects_without_recording_and_is_resettable(
    client: TestClient,
) -> None:
    mode_response = client.put(
        "/control/mode",
        json={"mode": "RETURN_500"},
    )

    assert mode_response.status_code == 200
    assert mode_response.json() == {
        "mode": "RETURN_500",
        "delay_seconds": 0.0,
    }
    assert client.get("/control/status").json() == {
        "mode": "RETURN_500",
        "delay_seconds": 0.0,
    }

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
    assert client.get("/control/status").json() == {
        "mode": "HEALTHY",
        "delay_seconds": 0.0,
    }


@mark.parametrize(
    ("mode", "expected_delay"),
    [
        ("SLOW", SLOW_DELAY_SECONDS),
        ("TIMEOUT", TIMEOUT_DELAY_SECONDS),
    ],
)
def test_delayed_modes_wait_then_accept_and_can_reset(
    client: TestClient,
    mode: str,
    expected_delay: float,
) -> None:
    mode_response = client.put("/control/mode", json={"mode": mode})

    assert mode_response.json() == {
        "mode": mode,
        "delay_seconds": expected_delay,
    }

    with patch("trackrelay.downstream.main.sleep") as simulated_sleep:
        delivery = client.post("/events", json=normalized_event_data())

    simulated_sleep.assert_called_once_with(expected_delay)
    assert delivery.status_code == 202
    assert client.get("/events").json() == [normalized_event_data()]

    reset_response = client.put("/control/mode", json={"mode": "HEALTHY"})
    assert reset_response.json() == {"mode": "HEALTHY", "delay_seconds": 0.0}


def test_unavailable_mode_returns_503_without_recording_and_can_reset(
    client: TestClient,
) -> None:
    mode_response = client.put(
        "/control/mode",
        json={"mode": "UNAVAILABLE"},
    )

    assert mode_response.json() == {
        "mode": "UNAVAILABLE",
        "delay_seconds": 0.0,
    }

    delivery = client.post("/events", json=normalized_event_data())

    assert delivery.status_code == 503
    assert delivery.json() == {"detail": "Simulated downstream unavailability"}
    assert client.get("/events").json() == []

    client.put("/control/mode", json={"mode": "HEALTHY"})
    assert client.post("/events", json=normalized_event_data()).status_code == 202
