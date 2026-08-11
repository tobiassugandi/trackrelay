"""Tests for the shipment event persistence model."""

from datetime import UTC, datetime
from uuid import UUID

from pytest import raises
from sqlalchemy.exc import IntegrityError

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import EventProcessingStatus, ShipmentStatus
from trackrelay.models import Event, Partner, Shipment

FIRST_EVENT_ID = UUID("00000000-0000-0000-0000-000000000001")
SECOND_EVENT_ID = UUID("00000000-0000-0000-0000-000000000002")
OCCURRED_AT = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 8, 6, 10, 0, 2, tzinfo=UTC)


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def add_parent_records(session) -> None:
    session.add(
        Partner(
            id="courier-alpha",
            name="Courier Alpha",
            adapter_type="courier-alpha",
        )
    )
    session.add(
        Shipment(
            tracking_number="TRK-001",
            current_status=ShipmentStatus.CREATED,
            current_status_occurred_at=OCCURRED_AT,
        )
    )


def build_event(event_id: UUID) -> Event:
    return Event(
        id=event_id,
        partner_id="courier-alpha",
        partner_event_id="ALPHA-001",
        tracking_number="TRK-001",
        status=ShipmentStatus.CREATED,
        occurred_at=OCCURRED_AT,
        received_at=RECEIVED_AT,
        raw_payload={"status": "CREATED"},
    )


def test_event_model_round_trip_uses_processing_defaults() -> None:
    engine, session_factory = create_test_database()

    with session_factory() as session:
        add_parent_records(session)
        session.add(build_event(FIRST_EVENT_ID))
        session.commit()

    with session_factory() as session:
        event = session.get(Event, FIRST_EVENT_ID)
        assert event is not None
        assert event.status is ShipmentStatus.CREATED
        assert event.processing_status is EventProcessingStatus.RECEIVED
        assert event.state_applied is False
        assert event.state_rejection_reason is None
        assert event.raw_payload == {"status": "CREATED"}

    engine.dispose()


def test_event_model_rejects_duplicate_partner_event_id() -> None:
    engine, session_factory = create_test_database()

    with session_factory() as session:
        add_parent_records(session)
        session.add(build_event(FIRST_EVENT_ID))
        session.commit()

    with session_factory() as session:
        session.add(build_event(SECOND_EVENT_ID))
        with raises(IntegrityError):
            session.commit()

    engine.dispose()
