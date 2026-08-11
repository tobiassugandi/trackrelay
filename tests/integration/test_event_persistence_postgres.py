"""PostgreSQL integration tests for transactional event persistence."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from pytest import fixture, mark
from sqlalchemy import delete, func, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.models import Event, Partner, Shipment
from trackrelay.services import EventPersistenceResult, persist_normalized_event

PARTNER_ID = "integration-courier-alpha"
TRACKING_NUMBER = "INTEGRATION-TRK-001"
PARTNER_EVENT_ID = "INTEGRATION-ALPHA-001"
CONCURRENT_TRACKING_NUMBER = "INTEGRATION-TRK-CONCURRENT"
CONCURRENT_PARTNER_EVENT_ID = "INTEGRATION-ALPHA-CONCURRENT"


def cleanup_records() -> None:
    with session_factory.begin() as session:
        session.execute(delete(Event).where(Event.partner_id == PARTNER_ID))
        session.execute(
            delete(Shipment).where(
                Shipment.tracking_number.in_(
                    [TRACKING_NUMBER, CONCURRENT_TRACKING_NUMBER]
                )
            )
        )
        session.execute(delete(Partner).where(Partner.id == PARTNER_ID))


@fixture
def configured_partner() -> Iterator[None]:
    assert engine.dialect.name == "postgresql"
    cleanup_records()
    with session_factory.begin() as session:
        session.add(
            Partner(
                id=PARTNER_ID,
                name="Integration Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
    try:
        yield
    finally:
        cleanup_records()


def build_event(
    status: ShipmentStatus,
    occurred_at: datetime,
    *,
    partner_event_id: str = PARTNER_EVENT_ID,
    tracking_number: str = TRACKING_NUMBER,
) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id=PARTNER_ID,
        partner_event_id=partner_event_id,
        tracking_number=tracking_number,
        status=status,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(seconds=2),
        raw_payload={"status": status.value},
    )


@mark.integration
def test_duplicate_event_rolls_back_the_shipment_update(
    configured_partner: None,
) -> None:
    first_time = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    original = persist_normalized_event(
        build_event(ShipmentStatus.CREATED, first_time)
    )

    duplicate_time = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
    duplicate = persist_normalized_event(
        build_event(ShipmentStatus.DELIVERED, duplicate_time)
    )

    assert original.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.event_id == original.event_id

    with session_factory() as session:
        shipment = session.get(Shipment, TRACKING_NUMBER)
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.CREATED
        assert shipment.current_status_occurred_at == first_time
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.partner_id == PARTNER_ID)
        ) == 1


@mark.integration
def test_concurrent_duplicates_resolve_to_one_original_event(
    configured_partner: None,
) -> None:
    event = build_event(
        ShipmentStatus.PICKED_UP,
        datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        partner_event_id=CONCURRENT_PARTNER_EVENT_ID,
        tracking_number=CONCURRENT_TRACKING_NUMBER,
    )
    start_together = Barrier(2)

    def persist_concurrently() -> EventPersistenceResult:
        start_together.wait()
        return persist_normalized_event(event)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: persist_concurrently(), range(2)))

    assert results[0].event_id == results[1].event_id
    assert sorted(result.duplicate for result in results) == [False, True]

    with session_factory() as session:
        shipment = session.get(Shipment, CONCURRENT_TRACKING_NUMBER)
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.PICKED_UP
        assert session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == CONCURRENT_PARTNER_EVENT_ID,
            )
        ) == 1
