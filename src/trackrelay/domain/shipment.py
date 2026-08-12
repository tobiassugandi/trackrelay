"""Shipment domain types."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ShipmentStatus(StrEnum):
    """A shipment's normalized status inside TrackRelay."""

    CREATED = "created"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"


class TransitionRejectionReason(StrEnum):
    """Why an event did not change the shipment's current state."""

    STALE_EVENT = "stale_event"
    NON_FORWARD_TRANSITION = "non_forward_transition"
    TERMINAL_STATE = "terminal_state"


@dataclass(frozen=True)
class ShipmentTransitionDecision:
    """The pure domain outcome of comparing an event with shipment state."""

    applied: bool
    rejection_reason: TransitionRejectionReason | None = None


STATUS_ORDER = {status: position for position, status in enumerate(ShipmentStatus)}


def evaluate_shipment_transition(
    *,
    current_status: ShipmentStatus,
    current_occurred_at: datetime,
    incoming_status: ShipmentStatus,
    incoming_occurred_at: datetime,
) -> ShipmentTransitionDecision:
    """Decide whether an incoming event may advance an existing shipment.

    Any strictly later status is forward progress, so missing intermediate courier
    updates do not block a shipment. For equal occurrence times, status order is a
    deterministic tie-breaker. Delivered is terminal.
    """
    if incoming_occurred_at < current_occurred_at:
        return ShipmentTransitionDecision(
            applied=False,
            rejection_reason=TransitionRejectionReason.STALE_EVENT,
        )

    if current_status is ShipmentStatus.DELIVERED:
        return ShipmentTransitionDecision(
            applied=False,
            rejection_reason=TransitionRejectionReason.TERMINAL_STATE,
        )

    if STATUS_ORDER[incoming_status] <= STATUS_ORDER[current_status]:
        return ShipmentTransitionDecision(
            applied=False,
            rejection_reason=TransitionRejectionReason.NON_FORWARD_TRANSITION,
        )

    return ShipmentTransitionDecision(applied=True)
