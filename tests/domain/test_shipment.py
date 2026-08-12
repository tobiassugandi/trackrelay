"""Tests for shipment domain types."""

from datetime import UTC, datetime, timedelta
from itertools import product

from pytest import mark

from trackrelay.domain import (
    ShipmentStatus,
    TransitionRejectionReason,
    evaluate_shipment_transition,
)

OCCURRED_AT = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


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


FORWARD_TRANSITIONS = [
    (current, incoming)
    for current, incoming in product(ShipmentStatus, repeat=2)
    if list(ShipmentStatus).index(incoming) > list(ShipmentStatus).index(current)
]


@mark.parametrize(("current", "incoming"), FORWARD_TRANSITIONS)
def test_any_strictly_forward_status_is_allowed(
    current: ShipmentStatus,
    incoming: ShipmentStatus,
) -> None:
    decision = evaluate_shipment_transition(
        current_status=current,
        current_occurred_at=OCCURRED_AT,
        incoming_status=incoming,
        incoming_occurred_at=OCCURRED_AT + timedelta(minutes=1),
    )

    assert decision.applied is True
    assert decision.rejection_reason is None


def test_a_later_event_cannot_repeat_or_reverse_status() -> None:
    for incoming in (ShipmentStatus.CREATED, ShipmentStatus.IN_TRANSIT):
        decision = evaluate_shipment_transition(
            current_status=ShipmentStatus.IN_TRANSIT,
            current_occurred_at=OCCURRED_AT,
            incoming_status=incoming,
            incoming_occurred_at=OCCURRED_AT + timedelta(minutes=1),
        )

        assert decision.applied is False
        assert (
            decision.rejection_reason
            is TransitionRejectionReason.NON_FORWARD_TRANSITION
        )


def test_status_order_breaks_an_equal_timestamp_tie() -> None:
    forward = evaluate_shipment_transition(
        current_status=ShipmentStatus.PICKED_UP,
        current_occurred_at=OCCURRED_AT,
        incoming_status=ShipmentStatus.IN_TRANSIT,
        incoming_occurred_at=OCCURRED_AT,
    )
    backward = evaluate_shipment_transition(
        current_status=ShipmentStatus.PICKED_UP,
        current_occurred_at=OCCURRED_AT,
        incoming_status=ShipmentStatus.CREATED,
        incoming_occurred_at=OCCURRED_AT,
    )
    repeated = evaluate_shipment_transition(
        current_status=ShipmentStatus.PICKED_UP,
        current_occurred_at=OCCURRED_AT,
        incoming_status=ShipmentStatus.PICKED_UP,
        incoming_occurred_at=OCCURRED_AT,
    )

    assert forward.applied is True
    assert backward.applied is False
    assert backward.rejection_reason is TransitionRejectionReason.NON_FORWARD_TRANSITION
    assert repeated.applied is False
    assert repeated.rejection_reason is TransitionRejectionReason.NON_FORWARD_TRANSITION


def test_delivered_is_terminal() -> None:
    decision = evaluate_shipment_transition(
        current_status=ShipmentStatus.DELIVERED,
        current_occurred_at=OCCURRED_AT,
        incoming_status=ShipmentStatus.DELIVERED,
        incoming_occurred_at=OCCURRED_AT + timedelta(minutes=1),
    )

    assert decision.applied is False
    assert decision.rejection_reason is TransitionRejectionReason.TERMINAL_STATE


def test_an_older_event_is_stale_even_after_delivery() -> None:
    decision = evaluate_shipment_transition(
        current_status=ShipmentStatus.DELIVERED,
        current_occurred_at=OCCURRED_AT,
        incoming_status=ShipmentStatus.OUT_FOR_DELIVERY,
        incoming_occurred_at=OCCURRED_AT - timedelta(minutes=1),
    )

    assert decision.applied is False
    assert decision.rejection_reason is TransitionRejectionReason.STALE_EVENT
