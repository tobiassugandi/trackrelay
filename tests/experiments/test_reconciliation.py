"""Tests for basic manifest-to-database reconciliation."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import ValidationError
from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.experiments.generator import (
    GeneratorConfiguration,
    InputManifest,
    ManifestEvent,
    generate_input_manifest,
    write_input_manifest,
)
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    fetch_simulator_receipts,
    load_input_manifest,
    reconcile_manifest,
    write_reconciliation_report,
)
from trackrelay.experiments.reconciliation import (
    TestRunDefinitionMismatchError as RunDefinitionMismatchError,
)
from trackrelay.experiments.reconciliation import (
    TestRunNotFoundError as RunNotFoundError,
)
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000703")


def build_manifest() -> InputManifest:
    return generate_input_manifest(
        seed=20260817,
        configuration=GeneratorConfiguration(
            partner_id="alpha-indonesia",
            shipment_count=1,
        ),
        test_run_id=TEST_RUN_ID,
    )


def create_test_database(manifest: InputManifest):
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    tracking_number = next(iter(manifest.expected_final_shipments))

    with sessions.begin() as session:
        session.add(
            ExperimentRunModel(
                id=manifest.test_run_id,
                scenario_name=manifest.scenario_name,
                random_seed=manifest.seed,
                configuration=manifest.configuration.model_dump(mode="json"),
                expected_event_count=manifest.events_generated,
            )
        )
        session.add(
            Partner(
                id=manifest.configuration.partner_id,
                name="Alpha Indonesia",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            Shipment(
                tracking_number=tracking_number,
                current_status=ShipmentStatus.DELIVERED,
                current_status_occurred_at=manifest.expected_events[-1]
                .expected_occurred_at,
            )
        )

    return engine, sessions


def add_manifest_event_to_database(
    manifest_event: ManifestEvent,
    *,
    processing_status: EventProcessingStatus,
    sessions,
    raw_payload: dict[str, object] | None = None,
) -> UUID:
    with sessions.begin() as session:
        database_event = Event(
            partner_id=manifest_event.partner_id,
            partner_event_id=manifest_event.partner_event_id,
            tracking_number=manifest_event.tracking_number,
            status=manifest_event.expected_status,
            occurred_at=manifest_event.expected_occurred_at,
            received_at=(manifest_event.expected_occurred_at + timedelta(seconds=1)),
            raw_payload=(
                manifest_event.payload if raw_payload is None else raw_payload
            ),
            test_run_id=manifest_event.test_run_id,
            processing_status=processing_status,
            state_applied=True,
        )
        session.add(database_event)
        session.flush()
        return database_event.id


def add_delivery_attempt(
    database_event_id: UUID,
    *,
    result: DeliveryAttemptResult,
    sessions,
) -> None:
    started_at = build_manifest().configuration.start_at
    with sessions.begin() as session:
        session.add(
            DeliveryAttempt(
                event_id=database_event_id,
                attempt_number=1,
                result=result,
                response_code=(
                    202 if result is DeliveryAttemptResult.DELIVERED else 503
                ),
                latency_ms=10,
                error=(
                    None
                    if result is DeliveryAttemptResult.DELIVERED
                    else "simulated failure"
                ),
                started_at=started_at,
                completed_at=started_at + timedelta(milliseconds=10),
            )
        )


def simulator_receipt_for(manifest_event: ManifestEvent) -> NormalizedEvent:
    return NormalizedEvent(
        partner_id=manifest_event.partner_id,
        partner_event_id=manifest_event.partner_event_id,
        tracking_number=manifest_event.tracking_number,
        status=manifest_event.expected_status,
        occurred_at=manifest_event.expected_occurred_at,
        received_at=manifest_event.expected_occurred_at + timedelta(seconds=1),
        raw_payload=manifest_event.payload,
        test_run_id=manifest_event.test_run_id,
    )


def add_complete_delivery_evidence(
    manifest: InputManifest,
    *,
    sessions,
) -> list[NormalizedEvent]:
    simulator_receipts = []
    for manifest_event in manifest.expected_events:
        database_event_id = add_manifest_event_to_database(
            manifest_event,
            processing_status=EventProcessingStatus.PROCESSED,
            sessions=sessions,
        )
        add_delivery_attempt(
            database_event_id,
            result=DeliveryAttemptResult.DELIVERED,
            sessions=sessions,
        )
        simulator_receipts.append(simulator_receipt_for(manifest_event))
    return simulator_receipts


def test_reconciliation_reports_request_and_processing_counts() -> None:
    manifest = build_manifest()
    engine, sessions = create_test_database(manifest)
    processing_statuses = (
        EventProcessingStatus.PROCESSED,
        EventProcessingStatus.PROCESSED,
        EventProcessingStatus.FAILED,
        EventProcessingStatus.RECEIVED,
    )
    simulator_receipts = []
    for manifest_event, processing_status in zip(
        manifest.expected_events,
        processing_statuses,
        strict=False,
    ):
        database_event_id = add_manifest_event_to_database(
            manifest_event,
            processing_status=processing_status,
            sessions=sessions,
        )
        if manifest_event.sequence_number == 1:
            add_delivery_attempt(
                database_event_id,
                result=DeliveryAttemptResult.DELIVERED,
                sessions=sessions,
            )
            simulator_receipts.append(simulator_receipt_for(manifest_event))
        elif manifest_event.sequence_number == 2:
            add_delivery_attempt(
                database_event_id,
                result=DeliveryAttemptResult.HTTP_ERROR,
                sessions=sessions,
            )

    with sessions() as session:
        report = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )

    assert report == ReconciliationReport(
        test_run_id=manifest.test_run_id,
        generated=5,
        accepted=4,
        rejected=1,
        unique=4,
        processed=2,
        failed=1,
        pending=1,
        unaccounted=0,
        simulator_receipts=1,
        simulator_unique_events=1,
    )
    engine.dispose()


def test_reconciliation_marks_mismatched_and_unexpected_rows_unaccounted() -> None:
    manifest = build_manifest()
    engine, sessions = create_test_database(manifest)
    simulator_receipts = []
    for manifest_event in manifest.expected_events:
        database_event_id = add_manifest_event_to_database(
            manifest_event,
            processing_status=EventProcessingStatus.PROCESSED,
            sessions=sessions,
            raw_payload=(
                {"unexpected": "payload"}
                if manifest_event.sequence_number == 1
                else None
            ),
        )
        add_delivery_attempt(
            database_event_id,
            result=DeliveryAttemptResult.DELIVERED,
            sessions=sessions,
        )
        simulator_receipts.append(simulator_receipt_for(manifest_event))

    first_manifest_event = manifest.expected_events[0]
    with sessions.begin() as session:
        session.add(
            Event(
                partner_id=first_manifest_event.partner_id,
                partner_event_id="UNEXPECTED-RUN-EVENT",
                tracking_number=first_manifest_event.tracking_number,
                status=ShipmentStatus.CREATED,
                occurred_at=first_manifest_event.expected_occurred_at,
                received_at=(
                    first_manifest_event.expected_occurred_at + timedelta(seconds=2)
                ),
                raw_payload={"event_id": "UNEXPECTED-RUN-EVENT"},
                test_run_id=manifest.test_run_id,
                processing_status=EventProcessingStatus.PROCESSED,
                state_applied=False,
            )
        )

    with sessions() as session:
        report = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )

    assert report.accepted == 5
    assert report.rejected == 0
    assert report.unique == 5
    assert report.processed == 5
    assert report.unaccounted == 2
    assert report.invariants_passed is False
    engine.dispose()


def test_reconciliation_detects_duplicate_effects_and_wrong_final_state() -> None:
    manifest = build_manifest()
    engine, sessions = create_test_database(manifest)
    simulator_receipts = add_complete_delivery_evidence(
        manifest,
        sessions=sessions,
    )
    simulator_receipts.append(simulator_receipt_for(manifest.expected_events[0]))

    tracking_number = next(iter(manifest.expected_final_shipments))
    with sessions.begin() as session:
        shipment = session.get(Shipment, tracking_number)
        assert shipment is not None
        shipment.current_status = ShipmentStatus.OUT_FOR_DELIVERY

    with sessions() as session:
        report = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )

    assert report.simulator_receipts == 6
    assert report.simulator_unique_events == 5
    assert report.duplicate_business_effects == 1
    assert report.incorrect_final_shipment_states == 1
    assert report.unaccounted == 1
    assert report.invariants_passed is False
    engine.dispose()


def test_wrong_final_shipment_state_alone_fails_reconciliation() -> None:
    manifest = build_manifest()
    engine, sessions = create_test_database(manifest)
    simulator_receipts = add_complete_delivery_evidence(
        manifest,
        sessions=sessions,
    )

    tracking_number = next(iter(manifest.expected_final_shipments))
    with sessions.begin() as session:
        database_shipment = session.get(Shipment, tracking_number)
        assert database_shipment is not None
        database_shipment.current_status = ShipmentStatus.OUT_FOR_DELIVERY

    with sessions() as session:
        report = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )

    assert report.unaccounted == 0
    assert report.duplicate_business_effects == 0
    assert report.incorrect_final_shipment_states == 1
    assert report.invariants_passed is False
    engine.dispose()


def test_fetch_simulator_receipts_validates_normalized_events() -> None:
    manifest_event = build_manifest().expected_events[0]
    simulator_receipt = simulator_receipt_for(manifest_event)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=[simulator_receipt.model_dump(mode="json")],
        )
    )

    with httpx.Client(transport=transport) as client:
        fetched_simulator_receipts = fetch_simulator_receipts(
            "http://downstream.test/",
            client=client,
        )

    assert fetched_simulator_receipts == (simulator_receipt,)


def test_reconciliation_requires_the_manifest_test_run() -> None:
    manifest = build_manifest()
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with sessions() as session, raises(RunNotFoundError, match=str(TEST_RUN_ID)):
        reconcile_manifest(manifest, session=session)

    engine.dispose()


def test_reconciliation_rejects_mismatched_test_run_metadata() -> None:
    manifest = build_manifest()
    engine, sessions = create_test_database(manifest)
    with sessions.begin() as session:
        database_test_run = session.get(
            ExperimentRunModel,
            manifest.test_run_id,
        )
        assert database_test_run is not None
        database_test_run.random_seed += 1

    with sessions() as session, raises(
        RunDefinitionMismatchError,
        match="random_seed",
    ):
        reconcile_manifest(manifest, session=session)

    engine.dispose()


def test_manifest_and_report_files_round_trip_through_their_schemas(
    tmp_path: Path,
) -> None:
    manifest = build_manifest()
    manifest_path = tmp_path / "input-manifest.json"
    report_path = tmp_path / "reconciliation.json"
    report = ReconciliationReport(
        test_run_id=manifest.test_run_id,
        generated=5,
        accepted=5,
        rejected=0,
        unique=5,
        processed=5,
        failed=0,
        pending=0,
        unaccounted=0,
    )

    write_input_manifest(manifest, manifest_path)
    write_reconciliation_report(report, report_path)

    assert load_input_manifest(manifest_path) == manifest
    assert ReconciliationReport.model_validate_json(
        report_path.read_text(encoding="utf-8")
    ) == report


def test_report_rejects_inconsistent_accounting() -> None:
    with raises(ValidationError, match="generated"):
        ReconciliationReport(
            test_run_id=TEST_RUN_ID,
            generated=5,
            accepted=4,
            rejected=0,
            unique=4,
            processed=4,
            failed=0,
            pending=0,
            unaccounted=0,
        )
