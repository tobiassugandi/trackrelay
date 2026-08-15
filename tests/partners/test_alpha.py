"""Tests for Courier Alpha's external contract and adapter."""

from datetime import datetime, timedelta

from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import (
    AlphaStatusCode,
    CourierAlphaAdapter,
    CourierAlphaPayload,
)

from .contract import PartnerAdapterContract


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


ALPHA_STATUS_FOR = {
    ShipmentStatus.CREATED: AlphaStatusCode.CREATED,
    ShipmentStatus.PICKED_UP: AlphaStatusCode.PICKED_UP,
    ShipmentStatus.IN_TRANSIT: AlphaStatusCode.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY: AlphaStatusCode.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED: AlphaStatusCode.DELIVERED,
}


class TestCourierAlphaAdapterContract(
    PartnerAdapterContract[CourierAlphaPayload]
):
    """Prove Courier Alpha satisfies every shared adapter invariant."""

    adapter = CourierAlphaAdapter()
    expected_adapter_type = "courier-alpha"
    expected_partner_id = "alpha-indonesia"
    expected_partner_event_id = "ALPHA-001842"
    expected_tracking_number = "ALP123456789"
    expected_occurred_at = datetime.fromisoformat("2026-08-06T14:21:00+07:00")

    def make_payload(self, status: ShipmentStatus) -> CourierAlphaPayload:
        data = valid_alpha_data()
        data["status"] = ALPHA_STATUS_FOR[status].value
        return self.adapter.payload_model.model_validate(data)
