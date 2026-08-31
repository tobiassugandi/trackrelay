"""Tests for durable downstream delivery-attempt recording."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from pytest import raises
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.services import deliver_and_record_normalized_event

STARTED_AT = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


def create_event_fixture() -> tuple[
    Engine,
    sessionmaker[Session],
    NormalizedEvent,
    UUID,
]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    normalized = NormalizedEvent(
        partner_id="courier-alpha",
        partner_event_id="ATTEMPT-EVENT-001",
        tracking_number="ATTEMPT-TRK-001",
        status=ShipmentStatus.PICKED_UP,
        occurred_at=STARTED_AT,
        received_at=STARTED_AT + timedelta(seconds=2),
        raw_payload={"status": "PICKUP"},
    )

    with sessions.begin() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            Shipment(
                tracking_number=normalized.tracking_number,
                current_status=normalized.status,
                current_status_occurred_at=normalized.occurred_at,
            )
        )
        session.flush()
        event = Event(
            partner_id=normalized.partner_id,
            partner_event_id=normalized.partner_event_id,
            tracking_number=normalized.tracking_number,
            status=normalized.status,
            occurred_at=normalized.occurred_at,
            received_at=normalized.received_at,
            raw_payload=normalized.raw_payload,
            processing_status=EventProcessingStatus.PROCESSED,
            state_applied=True,
        )
        session.add(event)
        session.flush()
        event_id = event.id

    return engine, sessions, normalized, event_id


def deterministic_clocks(
    *,
    latency_ms: int,
) -> tuple[Callable[[], float], Callable[[], datetime]]:
    wall_times = iter([STARTED_AT, STARTED_AT + timedelta(milliseconds=latency_ms)])
    monotonic_times = iter([10.0, 10.0 + latency_ms / 1000])
    return lambda: next(monotonic_times), lambda: next(wall_times)


def test_successful_delivery_records_all_attempt_fields() -> None:
    engine, sessions, normalized, event_id = create_event_fixture()
    monotonic, utcnow = deterministic_clocks(latency_ms=123)

    def accept(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == str(event_id)
        return httpx.Response(202)

    transport = httpx.MockTransport(accept)

    with httpx.Client(transport=transport) as client:
        result = deliver_and_record_normalized_event(
            normalized,
            event_id=event_id,
            downstream_url="http://downstream.test",
            client=client,
            sessions=sessions,
            monotonic=monotonic,
            utcnow=utcnow,
        )

    with sessions() as session:
        attempt = session.scalar(select(DeliveryAttempt))
        assert attempt is not None
        assert attempt.event_id == event_id
        assert attempt.attempt_number == 1
        assert attempt.result is DeliveryAttemptResult.DELIVERED
        assert attempt.response_code == 202
        assert attempt.latency_ms == 123
        assert attempt.error is None
        assert attempt.started_at.replace(tzinfo=UTC) == STARTED_AT
        assert attempt.completed_at.replace(tzinfo=UTC) == STARTED_AT + timedelta(
            milliseconds=123
        )
    assert result.downstream_status_code == 202
    engine.dispose()


def test_http_and_transport_failures_are_recorded_and_reraised() -> None:
    engine, sessions, normalized, event_id = create_event_fixture()
    http_monotonic, http_utcnow = deterministic_clocks(latency_ms=50)
    http_transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with httpx.Client(transport=http_transport) as client, raises(
        httpx.HTTPStatusError
    ):
        deliver_and_record_normalized_event(
            normalized,
            event_id=event_id,
            downstream_url="http://downstream.test",
            client=client,
            sessions=sessions,
            monotonic=http_monotonic,
            utcnow=http_utcnow,
        )

    timeout_monotonic, timeout_utcnow = deterministic_clocks(latency_ms=5000)

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(time_out)) as client, raises(
        httpx.ReadTimeout
    ):
        deliver_and_record_normalized_event(
            normalized,
            event_id=event_id,
            downstream_url="http://downstream.test",
            client=client,
            sessions=sessions,
            monotonic=timeout_monotonic,
            utcnow=timeout_utcnow,
        )

    with sessions() as session:
        attempts = tuple(
            session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.attempt_number)
            )
        )
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert [attempt.result for attempt in attempts] == [
            DeliveryAttemptResult.HTTP_ERROR,
            DeliveryAttemptResult.TRANSPORT_ERROR,
        ]
        assert [attempt.response_code for attempt in attempts] == [500, None]
        assert [attempt.latency_ms for attempt in attempts] == [50, 5000]
        assert all(attempt.error for attempt in attempts)
    engine.dispose()
