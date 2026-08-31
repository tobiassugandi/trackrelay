"""Publish transactionally recorded downstream-delivery work."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.models import DeliveryOutboxEntry
from trackrelay.services.delivery_queue import (
    DownstreamDeliveryJob,
    DownstreamDeliveryQueue,
)


class DeliveryOutboxEntryNotFoundError(LookupError):
    """Report an event accepted without its required durable outbox row."""


def publish_delivery_outbox_entry(
    event_id: UUID,
    delivery_queue: DownstreamDeliveryQueue,
    *,
    sessions: sessionmaker[Session] = session_factory,
) -> bool:
    """Publish one pending entry and mark it only after queue acceptance."""
    with sessions.begin() as session:
        entry = session.scalar(
            select(DeliveryOutboxEntry)
            .where(
                DeliveryOutboxEntry.event_id == event_id,
                DeliveryOutboxEntry.published_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        if entry is None:
            existing_event_id = session.scalar(
                select(DeliveryOutboxEntry.event_id).where(
                    DeliveryOutboxEntry.event_id == event_id
                )
            )
            if existing_event_id is None:
                raise DeliveryOutboxEntryNotFoundError(
                    f"Event {event_id} has no delivery outbox entry"
                )
            # A published entry needs no work; a locked pending entry is already
            # owned by another relay and must not delay the API response.
            return False

        delivery_queue.enqueue(DownstreamDeliveryJob(event_id=event_id))
        entry.published_at = session.scalar(select(func.now()))
    return True


def publish_pending_delivery_jobs(
    delivery_queue: DownstreamDeliveryQueue,
    *,
    batch_size: int = 10,
    sessions: sessionmaker[Session] = session_factory,
) -> int:
    """Publish a locked batch of pending entries for crash-safe relay."""
    if batch_size < 1:
        raise ValueError("delivery outbox batch size must be positive")

    with sessions.begin() as session:
        entries = tuple(
            session.scalars(
                select(DeliveryOutboxEntry)
                .where(DeliveryOutboxEntry.published_at.is_(None))
                .order_by(
                    DeliveryOutboxEntry.created_at,
                    DeliveryOutboxEntry.event_id,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for entry in entries:
            delivery_queue.enqueue(
                DownstreamDeliveryJob(event_id=entry.event_id)
            )
        published_at = session.scalar(select(func.now()))
        for entry in entries:
            entry.published_at = published_at
    return len(entries)
