"""Shared TrackRelay domain types."""

from trackrelay.domain.delivery import DeliveryAttemptResult
from trackrelay.domain.event import EventProcessingStatus, NormalizedEvent
from trackrelay.domain.shipment import (
    ShipmentStatus,
    ShipmentTransitionDecision,
    TransitionRejectionReason,
    evaluate_shipment_transition,
)

__all__ = [
    "DeliveryAttemptResult",
    "EventProcessingStatus",
    "NormalizedEvent",
    "ShipmentStatus",
    "ShipmentTransitionDecision",
    "TransitionRejectionReason",
    "evaluate_shipment_transition",
]
