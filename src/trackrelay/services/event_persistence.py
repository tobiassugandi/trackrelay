"""Transactional normalized-event persistence."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import EventProcessingStatus, NormalizedEvent
from trackrelay.models import Event, Shipment


@dataclass(frozen=True)
class EventPersistenceResult:
    """Identify whether persistence created or found the logical event."""

    event_id: UUID
    duplicate: bool


def persist_normalized_event(
    normalized_event: NormalizedEvent,
    *,
    sessions: sessionmaker[Session] = session_factory,
) -> EventPersistenceResult:
    """Create a logical event or resolve a uniqueness race to the original."""
    try:
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
    except IntegrityError:
        with sessions() as session:
            original_event_id = session.scalar(
                select(Event.id).where(
                    Event.partner_id == normalized_event.partner_id,
                    Event.partner_event_id == normalized_event.partner_event_id,
                )
            )
        if original_event_id is None:
            raise
        return EventPersistenceResult(event_id=original_event_id, duplicate=True)

    return EventPersistenceResult(event_id=event_id, duplicate=False)
