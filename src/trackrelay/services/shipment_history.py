"""Read-only shipment event history queries."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from trackrelay.models import Event, Shipment


def list_shipment_events(
    tracking_number: str,
    *,
    session: Session,
) -> tuple[Event, ...] | None:
    """Return one shipment's events in deterministic business-time order.

    ``None`` distinguishes a missing shipment from an existing shipment whose
    history is empty.
    """
    if session.get(Shipment, tracking_number) is None:
        return None

    statement = (
        select(Event)
        .where(Event.tracking_number == tracking_number)
        .order_by(
            Event.occurred_at.asc(),
            Event.received_at.asc(),
            Event.created_at.asc(),
            Event.id.asc(),
        )
    )
    return tuple(session.scalars(statement))
