"""PostgreSQL integration tests for transactional event persistence."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

from fastapi.testclient import TestClient
from pytest import fixture, mark
from sqlalchemy import delete, func, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import (
    NormalizedEvent,
    ShipmentStatus,
    TransitionRejectionReason,
)
from trackrelay.main import app
from trackrelay.models import Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.services import EventPersistenceResult, persist_normalized_event

PARTNER_ID = "integration-courier-alpha"
TRACKING_NUMBER = "INTEGRATION-TRK-001"
PARTNER_EVENT_ID = "INTEGRATION-ALPHA-001"
CONCURRENT_TRACKING_NUMBER = "INTEGRATION-TRK-CONCURRENT"
CONCURRENT_PARTNER_EVENT_ID = "INTEGRATION-ALPHA-CONCURRENT"
STALE_PARTNER_EVENT_ID = "INTEGRATION-ALPHA-STALE"
SYNTHETIC_PARTNER_EVENT_ID = "INTEGRATION-ALPHA-SYNTHETIC"
SYNTHETIC_TRACKING_NUMBER = "INTEGRATION-TRK-SYNTHETIC"
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000701")


def cleanup_records() -> None:
    with session_factory.begin() as session:
        session.execute(delete(Event).where(Event.partner_id == PARTNER_ID))
        session.execute(
            delete(Shipment).where(
                Shipment.tracking_number.in_(
                    [
                        TRACKING_NUMBER,
                        CONCURRENT_TRACKING_NUMBER,
                        SYNTHETIC_TRACKING_NUMBER,
                    ]
                )
            )
        )
        session.execute(delete(Partner).where(Partner.id == PARTNER_ID))
        session.execute(
            delete(ExperimentRunModel).where(ExperimentRunModel.id == TEST_RUN_ID)
        )


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
    test_run_id: UUID | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id=PARTNER_ID,
        partner_event_id=partner_event_id,
        tracking_number=tracking_number,
        status=status,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(seconds=2),
        raw_payload={"status": status.value},
        test_run_id=test_run_id,
    )


@mark.integration
def test_synthetic_event_references_its_postgres_test_run(
    configured_partner: None,
) -> None:
    with session_factory.begin() as session:
        session.add(
            ExperimentRunModel(
                id=TEST_RUN_ID,
                scenario_name="normal",
                random_seed=8675309,
                configuration={"events": 1},
                expected_event_count=1,
            )
        )

    result = persist_normalized_event(
        build_event(
            ShipmentStatus.CREATED,
            datetime(2026, 8, 15, 8, 0, tzinfo=UTC),
            partner_event_id=SYNTHETIC_PARTNER_EVENT_ID,
            tracking_number=SYNTHETIC_TRACKING_NUMBER,
            test_run_id=TEST_RUN_ID,
        )
    )

    with session_factory() as session:
        event = session.get(Event, result.event_id)
        assert event is not None
        assert event.test_run_id == TEST_RUN_ID


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


@mark.integration
def test_stale_event_is_retained_without_reversing_the_shipment(
    configured_partner: None,
) -> None:
    delivered_time = datetime(2026, 8, 6, 10, 3, tzinfo=UTC)
    stale_time = datetime(2026, 8, 6, 10, 1, tzinfo=UTC)

    delivered = persist_normalized_event(
        build_event(ShipmentStatus.DELIVERED, delivered_time)
    )
    stale = persist_normalized_event(
        build_event(
            ShipmentStatus.OUT_FOR_DELIVERY,
            stale_time,
            partner_event_id=STALE_PARTNER_EVENT_ID,
        )
    )

    with session_factory() as session:
        shipment = session.get(Shipment, TRACKING_NUMBER)
        delivered_event = session.get(Event, delivered.event_id)
        stale_event = session.get(Event, stale.event_id)

        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.DELIVERED
        assert shipment.current_status_occurred_at == delivered_time
        assert delivered_event is not None
        assert delivered_event.state_applied is True
        assert delivered_event.state_rejection_reason is None
        assert stale_event is not None
        assert stale_event.state_applied is False
        assert (
            stale_event.state_rejection_reason
            == TransitionRejectionReason.STALE_EVENT.value
        )
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.partner_id == PARTNER_ID)
        ) == 2

    with TestClient(app) as client:
        response = client.get(f"/api/v1/shipments/{TRACKING_NUMBER}/events")

    assert response.status_code == 200
    history = response.json()
    assert [item["partner_event_id"] for item in history] == [
        STALE_PARTNER_EVENT_ID,
        PARTNER_EVENT_ID,
    ]
    assert [item["state_applied"] for item in history] == [False, True]
    assert history[0]["state_rejection_reason"] == "stale_event"
