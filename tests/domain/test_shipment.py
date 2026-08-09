"""Tests for shipment domain types."""

from pytest import mark

from trackrelay.domain import ShipmentStatus


def test_shipment_status_has_the_five_initial_values() -> None:
    assert [status.value for status in ShipmentStatus] == [
        "created",
        "picked_up",
        "in_transit",
        "out_for_delivery",
        "delivered",
    ]


@mark.parametrize("value", [status.value for status in ShipmentStatus])
def test_shipment_status_parses_each_valid_value(value: str) -> None:
    assert ShipmentStatus(value).value == value
