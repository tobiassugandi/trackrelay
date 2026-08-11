"""Transactional normalized-event persistence."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import EventProcessingStatus, NormalizedEvent
from trackrelay.models import Event, Shipment


def persist_normalized_event(
    normalized_event: NormalizedEvent,
    *,
    sessions: sessionmaker[Session] = session_factory,
) -> UUID:
    """Persist an event and its shipment state in one transaction."""
    with sessions.begin() as session:
        shipment = session.get(Shipment, normalized_event.tracking_number)
        if shipment is None:
            shipment = Shipment(
                tracking_number=normalized_event.tracking_number,
                current_status=normalized_event.status,
                current_status_occurred_at=normalized_event.occurred_at,
            )
            session.add(shipment)
        else:
            shipment.current_status = normalized_event.status
            shipment.current_status_occurred_at = normalized_event.occurred_at

        session.flush()

        persisted_event = Event(
            partner_id=normalized_event.partner_id,
            partner_event_id=normalized_event.partner_event_id,
            tracking_number=normalized_event.tracking_number,
            status=normalized_event.status,
            occurred_at=normalized_event.occurred_at,
            received_at=normalized_event.received_at,
            raw_payload=normalized_event.raw_payload,
            processing_status=EventProcessingStatus.PROCESSED,
            state_applied=True,
        )
        session.add(persisted_event)
        session.flush()
        event_id = persisted_event.id

    return event_id
