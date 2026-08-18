"""PostgreSQL integration coverage for basic run reconciliation."""

from datetime import timedelta
from uuid import UUID

from pytest import mark
from sqlalchemy import delete, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.experiments.generator import (
    GeneratorConfiguration,
    generate_input_manifest,
)
from trackrelay.experiments.reconciliation import reconcile_manifest
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel

PARTNER_ID = "reconciliation-alpha"
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000704")
MANIFEST = generate_input_manifest(
    seed=20260817,
    configuration=GeneratorConfiguration(
        partner_id=PARTNER_ID,
        shipment_count=1,
    ),
    test_run_id=TEST_RUN_ID,
)


def cleanup_records() -> None:
    tracking_numbers = tuple(MANIFEST.expected_final_shipments)
    with session_factory.begin() as session:
        session.execute(
            delete(DeliveryAttempt).where(
                DeliveryAttempt.event_id.in_(
                    select(Event.id).where(Event.test_run_id == TEST_RUN_ID)
                )
            )
        )
        session.execute(delete(Event).where(Event.test_run_id == TEST_RUN_ID))
        session.execute(
            delete(Shipment).where(
                Shipment.tracking_number.in_(tracking_numbers)
            )
        )
        session.execute(delete(Partner).where(Partner.id == PARTNER_ID))
        session.execute(
            delete(ExperimentRunModel).where(
                ExperimentRunModel.id == TEST_RUN_ID
            )
        )


@mark.integration
def test_postgres_reconciliation_accounts_for_a_complete_manifest() -> None:
    assert engine.dialect.name == "postgresql"
    cleanup_records()
    tracking_number = next(iter(MANIFEST.expected_final_shipments))
    try:
        with session_factory.begin() as session:
            session.add(
                ExperimentRunModel(
                    id=MANIFEST.test_run_id,
                    scenario_name=MANIFEST.scenario_name,
                    random_seed=MANIFEST.seed,
                    configuration=MANIFEST.configuration.model_dump(mode="json"),
                    expected_event_count=MANIFEST.events_generated,
                )
            )
            session.add(
                Partner(
                    id=PARTNER_ID,
                    name="Reconciliation Alpha",
                    adapter_type="courier-alpha",
                )
            )
            session.add(
                Shipment(
                    tracking_number=tracking_number,
                    current_status=ShipmentStatus.DELIVERED,
                    current_status_occurred_at=MANIFEST.expected_events[-1]
                    .expected_occurred_at,
                )
            )
            session.flush()
            for manifest_event in MANIFEST.expected_events:
                database_event = Event(
                    partner_id=manifest_event.partner_id,
                    partner_event_id=manifest_event.partner_event_id,
                    tracking_number=manifest_event.tracking_number,
                    status=manifest_event.expected_status,
                    occurred_at=manifest_event.expected_occurred_at,
                    received_at=manifest_event.expected_occurred_at
                    + timedelta(seconds=1),
                    raw_payload=manifest_event.payload,
                    test_run_id=manifest_event.test_run_id,
                    processing_status=EventProcessingStatus.PROCESSED,
                    state_applied=True,
                )
                session.add(database_event)
                session.flush()
                session.add(
                    DeliveryAttempt(
                        event_id=database_event.id,
                        attempt_number=1,
                        result=DeliveryAttemptResult.DELIVERED,
                        response_code=202,
                        latency_ms=10,
                        error=None,
                        started_at=manifest_event.expected_occurred_at,
                        completed_at=manifest_event.expected_occurred_at
                        + timedelta(milliseconds=10),
                    )
                )

        simulator_receipts = tuple(
            NormalizedEvent(
                partner_id=manifest_event.partner_id,
                partner_event_id=manifest_event.partner_event_id,
                tracking_number=manifest_event.tracking_number,
                status=manifest_event.expected_status,
                occurred_at=manifest_event.expected_occurred_at,
                received_at=manifest_event.expected_occurred_at + timedelta(seconds=1),
                raw_payload=manifest_event.payload,
                test_run_id=manifest_event.test_run_id,
            )
            for manifest_event in MANIFEST.expected_events
        )

        with session_factory() as session:
            report = reconcile_manifest(
                MANIFEST,
                session=session,
                simulator_receipts=simulator_receipts,
            )

        assert report.generated == 5
        assert report.accepted == 5
        assert report.rejected == 0
        assert report.unique == 5
        assert report.processed == 5
        assert report.failed == 0
        assert report.pending == 0
        assert report.unaccounted == 0
        assert report.simulator_receipts == 5
        assert report.simulator_unique_events == 5
        assert report.duplicate_business_effects == 0
        assert report.incorrect_final_shipment_states == 0
        assert report.invariants_passed is True
    finally:
        cleanup_records()
