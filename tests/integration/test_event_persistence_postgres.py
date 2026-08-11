"""PostgreSQL integration tests for transactional event persistence."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from pytest import fixture, mark, raises
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from trackrelay.database import engine, session_factory
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.models import Event, Partner, Shipment
from trackrelay.services import persist_normalized_event

PARTNER_ID = "integration-courier-alpha"
TRACKING_NUMBER = "INTEGRATION-TRK-001"
PARTNER_EVENT_ID = "INTEGRATION-ALPHA-001"


def cleanup_records() -> None:
    with session_factory.begin() as session:
        session.execute(delete(Event).where(Event.partner_id == PARTNER_ID))
        session.execute(
            delete(Shipment).where(Shipment.tracking_number == TRACKING_NUMBER)
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


def build_event(status: ShipmentStatus, occurred_at: datetime) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id=PARTNER_ID,
        partner_event_id=PARTNER_EVENT_ID,
        tracking_number=TRACKING_NUMBER,
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
    persist_normalized_event(build_event(ShipmentStatus.CREATED, first_time))

    duplicate_time = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
    with raises(IntegrityError):
        persist_normalized_event(build_event(ShipmentStatus.DELIVERED, duplicate_time))

    with session_factory() as session:
        shipment = session.get(Shipment, TRACKING_NUMBER)
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.CREATED
        assert shipment.current_status_occurred_at == first_time
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.partner_id == PARTNER_ID)
        ) == 1
