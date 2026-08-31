"""Fast tests for transactional event persistence."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import EventProcessingStatus, NormalizedEvent, ShipmentStatus
from trackrelay.models import DeliveryOutboxEntry, Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.services import persist_normalized_event


def build_event(
    partner_event_id: str,
    status: ShipmentStatus,
    occurred_at: datetime,
    *,
    partner_id: str = "courier-alpha",
    tracking_number: str = "TRK-001",
    test_run_id: UUID | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id=partner_id,
        partner_event_id=partner_event_id,
        tracking_number=tracking_number,
        status=status,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(seconds=2),
        raw_payload={"status": status.value},
        test_run_id=test_run_id,
    )


def test_persistence_creates_then_updates_a_shipment_atomically() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    first_time = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    second_time = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)

    with sessions.begin() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )

    first_result = persist_normalized_event(
        build_event("ALPHA-001", ShipmentStatus.PICKED_UP, first_time),
        sessions=sessions,
    )
    second_result = persist_normalized_event(
        build_event("ALPHA-002", ShipmentStatus.IN_TRANSIT, second_time),
        sessions=sessions,
    )

    with sessions() as session:
        shipment = session.get(Shipment, "TRK-001")
        first_event = session.get(Event, first_result.event_id)
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.IN_TRANSIT
        assert shipment.current_status_occurred_at.replace(tzinfo=UTC) == second_time
        assert first_event is not None
        assert first_event.processing_status is EventProcessingStatus.PROCESSED
        assert first_event.state_applied is True
        assert session.scalar(select(func.count()).select_from(Event)) == 2
        assert (
            session.scalar(select(func.count()).select_from(DeliveryOutboxEntry))
            == 2
        )
        assert session.get(DeliveryOutboxEntry, first_result.event_id) is not None
        assert session.get(DeliveryOutboxEntry, second_result.event_id) is not None
        assert first_result.duplicate is False
        assert second_result.duplicate is False
        assert first_result.event_id != second_result.event_id

    engine.dispose()


def test_persistence_attaches_a_synthetic_event_to_its_test_run() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    test_run_id = UUID("00000000-0000-0000-0000-000000000701")
    occurred_at = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)

    with sessions.begin() as session:
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            ExperimentRunModel(
                id=test_run_id,
                scenario_name="normal",
                random_seed=8675309,
                configuration={"events": 1},
                expected_event_count=1,
            )
        )

    result = persist_normalized_event(
        build_event(
            "ALPHA-SYNTHETIC-001",
            ShipmentStatus.CREATED,
            occurred_at,
            test_run_id=test_run_id,
        ),
        sessions=sessions,
    )

    with sessions() as session:
        event = session.get(Event, result.event_id)
        assert event is not None
        assert event.test_run_id == test_run_id

    engine.dispose()


def test_partner_event_identity_is_scoped_to_the_business_partner() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    occurred_at = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    with sessions.begin() as session:
        session.add_all(
            [
                Partner(
                    id="alpha-indonesia",
                    name="Alpha Indonesia",
                    adapter_type="courier-alpha",
                ),
                Partner(
                    id="alpha-singapore",
                    name="Alpha Singapore",
                    adapter_type="courier-alpha",
                ),
            ]
        )

    indonesia = persist_normalized_event(
        build_event(
            "SHARED-EXTERNAL-ID",
            ShipmentStatus.CREATED,
            occurred_at,
            partner_id="alpha-indonesia",
            tracking_number="ID-TRK-001",
        ),
        sessions=sessions,
    )
    singapore = persist_normalized_event(
        build_event(
            "SHARED-EXTERNAL-ID",
            ShipmentStatus.CREATED,
            occurred_at,
            partner_id="alpha-singapore",
            tracking_number="SG-TRK-001",
        ),
        sessions=sessions,
    )

    with sessions() as session:
        events = tuple(session.scalars(select(Event).order_by(Event.partner_id)))
        assert [event.partner_id for event in events] == [
            "alpha-indonesia",
            "alpha-singapore",
        ]
        assert indonesia.duplicate is False
        assert singapore.duplicate is False
        assert indonesia.event_id != singapore.event_id

    engine.dispose()
