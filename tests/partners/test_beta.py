"""Tests for Courier Beta's external contract and adapter."""

from datetime import UTC, datetime

from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.domain import ShipmentStatus
from trackrelay.partners import (
    BetaStatusCode,
    CourierBetaAdapter,
    CourierBetaPayload,
)

from .contract import PartnerAdapterContract


def valid_beta_data() -> dict[str, str | int]:
    return {
        "messageId": "beta-7741",
        "awb": "BET987654321",
        "statusCode": 72,
        "timestamp": 1786000860,
    }


def test_beta_status_codes_are_explicit_and_stable() -> None:
    assert [(status.name, status.value) for status in BetaStatusCode] == [
        ("CREATED", 10),
        ("PICKED_UP", 20),
        ("IN_TRANSIT", 30),
        ("OUT_FOR_DELIVERY", 60),
        ("DELIVERED", 72),
    ]


def test_courier_beta_payload_parses_the_canonical_example() -> None:
    payload = CourierBetaPayload.model_validate(valid_beta_data())

    assert payload.message_id == "beta-7741"
    assert payload.awb == "BET987654321"
    assert payload.status_code is BetaStatusCode.DELIVERED
    assert payload.timestamp == 1786000860


@mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("messageId", "   "),
        ("statusCode", "72"),
        ("statusCode", 999),
        ("timestamp", "1786000860"),
        ("timestamp", -1),
    ],
)
def test_courier_beta_payload_rejects_invalid_values(
    field_name: str,
    invalid_value: str | int,
) -> None:
    data = valid_beta_data()
    data[field_name] = invalid_value

    with raises(ValidationError):
        CourierBetaPayload.model_validate(data)


def test_courier_beta_payload_rejects_unknown_fields() -> None:
    data = valid_beta_data()
    data["unexpected"] = "value"

    with raises(ValidationError):
        CourierBetaPayload.model_validate(data)


BETA_STATUS_FOR = {
    ShipmentStatus.CREATED: BetaStatusCode.CREATED,
    ShipmentStatus.PICKED_UP: BetaStatusCode.PICKED_UP,
    ShipmentStatus.IN_TRANSIT: BetaStatusCode.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY: BetaStatusCode.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED: BetaStatusCode.DELIVERED,
}


class TestCourierBetaAdapterContract(PartnerAdapterContract[CourierBetaPayload]):
    """Prove Courier Beta satisfies every shared adapter invariant."""

    adapter = CourierBetaAdapter()
    expected_adapter_type = "courier-beta"
    expected_partner_id = "beta-indonesia"
    expected_partner_event_id = "beta-7741"
    expected_tracking_number = "BET987654321"
    expected_occurred_at = datetime.fromtimestamp(1786000860, tz=UTC)

    def make_payload(self, status: ShipmentStatus) -> CourierBetaPayload:
        data = valid_beta_data()
        data["statusCode"] = BETA_STATUS_FOR[status].value
        return self.adapter.payload_model.model_validate(data)
