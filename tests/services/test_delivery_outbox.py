"""Tests for durable downstream-delivery outbox publication."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pytest import raises
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.models import DeliveryOutboxEntry, Partner
from trackrelay.services import (
    DeliveryOutboxEntryNotFoundError,
    DownstreamDeliveryJob,
    DownstreamDeliveryQueueError,
    RecordingDownstreamDeliveryQueue,
    persist_normalized_event,
    publish_delivery_outbox_entry,
    publish_pending_delivery_jobs,
)


def normalized_event(partner_event_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id="courier-alpha",
        partner_event_id=partner_event_id,
        tracking_number=f"TRK-{partner_event_id}",
        status=ShipmentStatus.PICKED_UP,
        occurred_at=datetime(2026, 8, 31, 7, 59, tzinfo=UTC),
        received_at=datetime(2026, 8, 31, 7, 59, 1, tzinfo=UTC),
        raw_payload={"status": "PICKUP"},
    )


def outbox_fixture() -> tuple[
    Engine,
    sessionmaker[Session],
    tuple[UUID, UUID],
]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
    event_ids = tuple(
        persist_normalized_event(
            normalized_event(partner_event_id),
            sessions=sessions,
        ).event_id
        for partner_event_id in ("OUTBOX-001", "OUTBOX-002")
    )
    return engine, sessions, event_ids


def test_single_entry_is_marked_only_after_queue_acceptance() -> None:
    engine, sessions, event_ids = outbox_fixture()
    queue = RecordingDownstreamDeliveryQueue()

    published = publish_delivery_outbox_entry(
        event_ids[0],
        queue,
        sessions=sessions,
    )
    replayed = publish_delivery_outbox_entry(
        event_ids[0],
        queue,
        sessions=sessions,
    )

    with sessions() as session:
        entry = session.get(DeliveryOutboxEntry, event_ids[0])
        assert entry is not None
        assert entry.published_at is not None
    assert published is True
    assert replayed is False
    assert queue.enqueued_jobs == (DownstreamDeliveryJob(event_id=event_ids[0]),)
    engine.dispose()


def test_missing_outbox_entry_is_an_acceptance_invariant_failure() -> None:
    engine, sessions, _ = outbox_fixture()

    with raises(DeliveryOutboxEntryNotFoundError, match="has no delivery"):
        publish_delivery_outbox_entry(
            uuid4(),
            RecordingDownstreamDeliveryQueue(),
            sessions=sessions,
        )

    engine.dispose()


def test_queue_failure_leaves_the_outbox_entry_pending_for_relay() -> None:
    engine, sessions, event_ids = outbox_fixture()

    class FailingQueue:
        def enqueue(self, job: DownstreamDeliveryJob) -> None:
            raise DownstreamDeliveryQueueError("simulated SQS failure")

    with raises(DownstreamDeliveryQueueError, match="simulated SQS failure"):
        publish_delivery_outbox_entry(
            event_ids[0],
            FailingQueue(),
            sessions=sessions,
        )

    with sessions() as session:
        entry = session.get(DeliveryOutboxEntry, event_ids[0])
        assert entry is not None
        assert entry.published_at is None
    engine.dispose()


def test_ambiguous_queue_failure_can_republish_only_a_safe_duplicate_job() -> None:
    engine, sessions, event_ids = outbox_fixture()

    class AmbiguousQueue(RecordingDownstreamDeliveryQueue):
        def __init__(self) -> None:
            super().__init__()
            self._fail_next = True

        def enqueue(self, job: DownstreamDeliveryJob) -> None:
            super().enqueue(job)
            if self._fail_next:
                self._fail_next = False
                raise DownstreamDeliveryQueueError(
                    "SQS outcome was not observed"
                )

    queue = AmbiguousQueue()
    with raises(DownstreamDeliveryQueueError, match="not observed"):
        publish_delivery_outbox_entry(
            event_ids[0],
            queue,
            sessions=sessions,
        )

    published_count = publish_pending_delivery_jobs(
        queue,
        sessions=sessions,
    )

    assert published_count == 2
    assert [job.event_id for job in queue.enqueued_jobs].count(event_ids[0]) == 2
    engine.dispose()


def test_pending_relay_publishes_a_bounded_batch() -> None:
    engine, sessions, event_ids = outbox_fixture()
    queue = RecordingDownstreamDeliveryQueue()

    first_count = publish_pending_delivery_jobs(
        queue,
        batch_size=1,
        sessions=sessions,
    )
    second_count = publish_pending_delivery_jobs(
        queue,
        batch_size=1,
        sessions=sessions,
    )
    final_count = publish_pending_delivery_jobs(
        queue,
        batch_size=1,
        sessions=sessions,
    )

    with sessions() as session:
        entries = tuple(
            session.scalars(
                select(DeliveryOutboxEntry).order_by(
                    DeliveryOutboxEntry.event_id
                )
            )
        )
        assert all(entry.published_at is not None for entry in entries)
    assert (first_count, second_count, final_count) == (1, 1, 0)
    assert {job.event_id for job in queue.enqueued_jobs} == set(event_ids)
    engine.dispose()
