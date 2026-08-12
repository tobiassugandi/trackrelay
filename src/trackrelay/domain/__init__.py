"""Shared TrackRelay domain types."""

from trackrelay.domain.event import EventProcessingStatus, NormalizedEvent
from trackrelay.domain.shipment import (
    ShipmentStatus,
    ShipmentTransitionDecision,
    TransitionRejectionReason,
    evaluate_shipment_transition,
)

__all__ = [
    "EventProcessingStatus",
    "NormalizedEvent",
    "ShipmentStatus",
    "ShipmentTransitionDecision",
    "TransitionRejectionReason",
    "evaluate_shipment_transition",
]
