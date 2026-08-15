"""Tests for the normalized event schema."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.domain import NormalizedEvent, ShipmentStatus


def valid_event_data() -> dict[str, object]:
    return {
        "partner_id": "courier-alpha",
        "partner_event_id": "ALPHA-001",
        "tracking_number": "TRK-001",
        "status": "picked_up",
        "occurred_at": datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
        "received_at": datetime(2026, 8, 6, 10, 0, 2, tzinfo=UTC),
        "raw_payload": {"status": "PICKUP", "attempt": 1},
    }


def test_normalized_event_keeps_occurrence_and_receipt_times_separate() -> None:
    event = NormalizedEvent.model_validate(valid_event_data())

    assert event.status is ShipmentStatus.PICKED_UP
    assert event.occurred_at == datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    assert event.received_at == datetime(2026, 8, 6, 10, 0, 2, tzinfo=UTC)
    assert event.received_at > event.occurred_at
    assert event.raw_payload == {"status": "PICKUP", "attempt": 1}
    assert event.test_run_id is None


def test_normalized_event_accepts_an_optional_test_run_id() -> None:
    test_run_id = UUID("00000000-0000-0000-0000-000000000701")
    data = valid_event_data()
    data["test_run_id"] = test_run_id

    event = NormalizedEvent.model_validate(data)

    assert event.test_run_id == test_run_id


@mark.parametrize("field_name", ["occurred_at", "received_at"])
def test_normalized_event_rejects_naive_timestamps(field_name: str) -> None:
    data = valid_event_data()
    data[field_name] = datetime(2026, 8, 6, 10, 0)  # noqa: DTZ001

    with raises(ValidationError):
        NormalizedEvent.model_validate(data)


@mark.parametrize("field_name", ["partner_id", "partner_event_id", "tracking_number"])
def test_normalized_event_rejects_blank_identifiers(field_name: str) -> None:
    data = valid_event_data()
    data[field_name] = "   "

    with raises(ValidationError):
        NormalizedEvent.model_validate(data)
