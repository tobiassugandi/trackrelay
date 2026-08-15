"""Tests for Courier Gamma's nested external contract and adapter."""

from datetime import UTC, datetime

from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import (
    CourierGammaAdapter,
    CourierGammaPayload,
    GammaStatusCode,
)

from .contract import PartnerAdapterContract


def valid_gamma_data() -> dict[str, object]:
    return {
        "notification": {
            "reference": "gamma-9012",
            "trackingNumber": "GAM246813579",
            "status": "DELIVERED",
            "occurredAt": "2026-08-06T07:21:00Z",
        }
    }


def test_gamma_status_codes_match_the_nested_external_contract() -> None:
    assert [status.value for status in GammaStatusCode] == [
        "CREATED",
        "PICKED_UP",
        "IN_TRANSIT",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
    ]


def test_courier_gamma_payload_parses_a_valid_nested_example() -> None:
    payload = CourierGammaPayload.model_validate(valid_gamma_data())

    assert payload.notification.reference == "gamma-9012"
    assert payload.notification.tracking_number == "GAM246813579"
    assert payload.notification.status is GammaStatusCode.DELIVERED
    assert payload.notification.occurred_at == datetime(
        2026,
        8,
        6,
        7,
        21,
        tzinfo=UTC,
    )


@mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("reference", "   "),
        ("status", "UNKNOWN"),
        ("occurredAt", "2026-08-06T14:21:00+07:00"),
        ("occurredAt", "2026-08-06T07:21:00"),
        ("occurredAt", 1786000860),
    ],
)
def test_courier_gamma_payload_rejects_invalid_nested_values(
    field_name: str,
    invalid_value: str | int,
) -> None:
    data = valid_gamma_data()
    notification = data["notification"]
    assert isinstance(notification, dict)
    notification[field_name] = invalid_value

    with raises(ValidationError):
        CourierGammaPayload.model_validate(data)


def test_courier_gamma_payload_rejects_unknown_nested_fields() -> None:
    data = valid_gamma_data()
    notification = data["notification"]
    assert isinstance(notification, dict)
    notification["unexpected"] = "value"

    with raises(ValidationError):
        CourierGammaPayload.model_validate(data)


GAMMA_STATUS_FOR = {
    ShipmentStatus.CREATED: GammaStatusCode.CREATED,
    ShipmentStatus.PICKED_UP: GammaStatusCode.PICKED_UP,
    ShipmentStatus.IN_TRANSIT: GammaStatusCode.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY: GammaStatusCode.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED: GammaStatusCode.DELIVERED,
}


class TestCourierGammaAdapterContract(
    PartnerAdapterContract[CourierGammaPayload]
):
    """Prove Courier Gamma satisfies every shared adapter invariant."""

    adapter = CourierGammaAdapter()
    expected_adapter_type = "courier-gamma"
    expected_payload_model = CourierGammaPayload
    expected_partner_id = "gamma-indonesia"
    expected_partner_event_id = "gamma-9012"
    expected_tracking_number = "GAM246813579"
    expected_occurred_at = datetime(2026, 8, 6, 7, 21, tzinfo=UTC)

    def make_payload(self, status: ShipmentStatus) -> CourierGammaPayload:
        data = valid_gamma_data()
        notification = data["notification"]
        assert isinstance(notification, dict)
        notification["status"] = GAMMA_STATUS_FOR[status].value
        return self.expected_payload_model.model_validate(data)
