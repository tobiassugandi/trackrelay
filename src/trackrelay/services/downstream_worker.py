"""Transport-neutral downstream worker behavior."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import DeliveryAttemptResult, NormalizedEvent
from trackrelay.models import DeliveryAttempt, Event
from trackrelay.services.delivery_queue import DownstreamDeliveryMessage
from trackrelay.services.downstream_delivery import DeliveryResult

PersistedEventLoader = Callable[[UUID], NormalizedEvent]
RecordedEventDeliverer = Callable[[NormalizedEvent, UUID], DeliveryResult]
RecordedDeliveryLoader = Callable[[UUID], DeliveryResult | None]


class PersistedEventNotFoundError(LookupError):
    """Report a queue job whose authoritative event does not exist."""


class RecordedDeliveryInconsistentError(RuntimeError):
    """Report a successful attempt that lacks its downstream response code."""


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


def load_recorded_delivery_result(
    event_id: UUID,
    *,
    sessions: sessionmaker[Session] = session_factory,
) -> DeliveryResult | None:
    """Return an earlier successful delivery so a replay can skip its side effect."""
    with sessions() as session:
        attempt = session.scalar(
            select(DeliveryAttempt)
            .where(
                DeliveryAttempt.event_id == event_id,
                DeliveryAttempt.result == DeliveryAttemptResult.DELIVERED,
            )
            .order_by(DeliveryAttempt.attempt_number.desc())
            .limit(1)
        )
    if attempt is None:
        return None
    if attempt.response_code is None:
        raise RecordedDeliveryInconsistentError(
            f"Delivered attempt for event {event_id} has no response code"
        )
    return DeliveryResult(downstream_status_code=attempt.response_code)


def process_downstream_delivery_message(
    message: DownstreamDeliveryMessage,
    *,
    deliver_and_record_event: RecordedEventDeliverer,
    load_event: PersistedEventLoader = load_persisted_normalized_event,
    load_recorded_delivery: RecordedDeliveryLoader = load_recorded_delivery_result,
) -> DeliveryResult:
    """Deliver one persisted event and acknowledge only after recorded success.

    Loading, delivery, attempt recording, and acknowledgement failures propagate.
    An unacknowledged transport can therefore make the message visible again.
    """
    event_id = message.job.event_id
    recorded_delivery = load_recorded_delivery(event_id)
    if recorded_delivery is not None:
        message.acknowledge()
        return recorded_delivery
    normalized_event = load_event(event_id)
    result = deliver_and_record_event(normalized_event, event_id)
    message.acknowledge()
    return result
