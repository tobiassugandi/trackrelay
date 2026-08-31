"""Transport-neutral downstream worker behavior."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import NormalizedEvent
from trackrelay.models import Event
from trackrelay.services.delivery_queue import DownstreamDeliveryMessage
from trackrelay.services.downstream_delivery import DeliveryResult

PersistedEventLoader = Callable[[UUID], NormalizedEvent]
RecordedEventDeliverer = Callable[[NormalizedEvent, UUID], DeliveryResult]


class PersistedEventNotFoundError(LookupError):
    """Report a queue job whose authoritative event does not exist."""


def _aware(timestamp: datetime) -> datetime:
    """Restore UTC awareness for databases that return naive timestamps."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp


def load_persisted_normalized_event(
    event_id: UUID,
    *,
    sessions: sessionmaker[Session] = session_factory,
) -> NormalizedEvent:
    """Load the authoritative normalized event referenced by a queue job."""
    with sessions() as session:
        event = session.get(Event, event_id)
        if event is None:
            raise PersistedEventNotFoundError(
                f"Persisted event {event_id} does not exist"
            )
        return NormalizedEvent(
            partner_id=event.partner_id,
            partner_event_id=event.partner_event_id,
            tracking_number=event.tracking_number,
            status=event.status,
            occurred_at=_aware(event.occurred_at),
            received_at=_aware(event.received_at),
            raw_payload=event.raw_payload,
            test_run_id=event.test_run_id,
        )


def process_downstream_delivery_message(
    message: DownstreamDeliveryMessage,
    *,
    deliver_and_record_event: RecordedEventDeliverer,
    load_event: PersistedEventLoader = load_persisted_normalized_event,
) -> DeliveryResult:
    """Deliver one persisted event and acknowledge only after recorded success.

    Loading, delivery, attempt recording, and acknowledgement failures propagate.
    An unacknowledged transport can therefore make the message visible again.
    """
    event_id = message.job.event_id
    normalized_event = load_event(event_id)
    result = deliver_and_record_event(normalized_event, event_id)
    message.acknowledge()
    return result
