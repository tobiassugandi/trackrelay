"""Tests for Courier Alpha's external contract and adapter."""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import (
    AlphaStatusCode,
    CourierAlphaAdapter,
    CourierAlphaPayload,
)


def valid_alpha_data() -> dict[str, str]:
    return {
        "event_id": "ALPHA-001842",
        "tracking_number": "ALP123456789",
        "status": "POD",
        "event_time": "2026-08-06T14:21:00+07:00",
    }


def test_alpha_status_codes_match_the_external_contract() -> None:
    assert [status.value for status in AlphaStatusCode] == [
        "CREATED",
        "PICKUP",
        "TRANSIT",
        "OFD",
        "POD",
    ]


def test_courier_alpha_payload_parses_a_valid_example() -> None:
    payload = CourierAlphaPayload.model_validate(valid_alpha_data())

    assert payload.event_id == "ALPHA-001842"
    assert payload.tracking_number == "ALP123456789"
    assert payload.status is AlphaStatusCode.DELIVERED
    assert payload.event_time.utcoffset() == timedelta(hours=7)


@mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("event_id", "   "),
        ("status", "UNKNOWN"),
        ("event_time", "2026-08-06T14:21:00"),
    ],
)
def test_courier_alpha_payload_rejects_invalid_values(
    field_name: str,
    invalid_value: str,
) -> None:
    data = valid_alpha_data()
    data[field_name] = invalid_value

    with raises(ValidationError):
        CourierAlphaPayload.model_validate(data)


def test_courier_alpha_payload_rejects_unknown_fields() -> None:
    data = valid_alpha_data()
    data["unexpected"] = "value"

    with raises(ValidationError):
        CourierAlphaPayload.model_validate(data)


@mark.parametrize(
    ("alpha_status", "normalized_status"),
    [
        (AlphaStatusCode.CREATED, ShipmentStatus.CREATED),
        (AlphaStatusCode.PICKED_UP, ShipmentStatus.PICKED_UP),
        (AlphaStatusCode.IN_TRANSIT, ShipmentStatus.IN_TRANSIT),
        (AlphaStatusCode.OUT_FOR_DELIVERY, ShipmentStatus.OUT_FOR_DELIVERY),
        (AlphaStatusCode.DELIVERED, ShipmentStatus.DELIVERED),
    ],
)
def test_courier_alpha_adapter_normalizes_each_status(
    alpha_status: AlphaStatusCode,
    normalized_status: ShipmentStatus,
) -> None:
    data = valid_alpha_data()
    data["status"] = alpha_status.value
    payload = CourierAlphaPayload.model_validate(data)
    received_at = datetime(2026, 8, 6, 7, 21, 2, tzinfo=UTC)

    event = CourierAlphaAdapter().normalize(payload, received_at=received_at)

    assert event.partner_id == "courier-alpha"
    assert event.partner_event_id == "ALPHA-001842"
    assert event.tracking_number == "ALP123456789"
    assert event.status is normalized_status
    assert event.occurred_at == payload.event_time
    assert event.received_at == received_at
    assert event.raw_payload["status"] == alpha_status.value
