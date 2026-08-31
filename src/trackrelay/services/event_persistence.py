"""Transactional normalized-event persistence."""

from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import (
    EventProcessingStatus,
    NormalizedEvent,
    evaluate_shipment_transition,
)
from trackrelay.models import DeliveryOutboxEntry, Event, Shipment


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
            shipment = session.scalar(
                select(Shipment)
                .where(
                    Shipment.tracking_number == normalized_event.tracking_number
                )
                .with_for_update()
            )
            state_applied = True
            state_rejection_reason: str | None = None

            if shipment is None:
                shipment = Shipment(
                    tracking_number=normalized_event.tracking_number,
                    current_status=normalized_event.status,
                    current_status_occurred_at=normalized_event.occurred_at,
                )
                session.add(shipment)
            else:
                current_occurred_at = shipment.current_status_occurred_at
                if current_occurred_at.tzinfo is None:
                    current_occurred_at = current_occurred_at.replace(tzinfo=UTC)

                decision = evaluate_shipment_transition(
                    current_status=shipment.current_status,
                    current_occurred_at=current_occurred_at,
                    incoming_status=normalized_event.status,
                    incoming_occurred_at=normalized_event.occurred_at,
                )
                state_applied = decision.applied
                if decision.rejection_reason is not None:
                    state_rejection_reason = decision.rejection_reason.value

                if decision.applied:
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
                test_run_id=normalized_event.test_run_id,
                processing_status=EventProcessingStatus.PROCESSED,
                state_applied=state_applied,
                state_rejection_reason=state_rejection_reason,
            )
            session.add(persisted_event)
            session.flush()
            event_id = persisted_event.id
            session.add(DeliveryOutboxEntry(event_id=event_id))
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
