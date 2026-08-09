"""Shipment domain types."""

from enum import StrEnum


class ShipmentStatus(StrEnum):
    """A shipment's normalized status inside TrackRelay."""

    CREATED = "created"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
