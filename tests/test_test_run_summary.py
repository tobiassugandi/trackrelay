"""Tests for database-backed experiment-run summaries."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pytest import fixture

import trackrelay.main as trackrelay_main
from trackrelay.database import (
    Base,
    create_database_engine,
    create_session_factory,
    get_session,
)
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    ShipmentStatus,
)
from trackrelay.experiments.generator import (
    GeneratorConfiguration,
    generate_input_manifest,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.main import app
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Partner,
    Shipment,
)
from trackrelay.models import TestRun as ExperimentRunModel

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000705")
EMPTY_TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000706")
STARTED_AT = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(minutes=2)


@fixture
def summary_client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'summary.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with sessions.begin() as session:
        session.add_all(
            [
                ExperimentRunModel(
                    id=TEST_RUN_ID,
                    scenario_name="normal",
                    random_seed=20260818,
                    configuration={
                        "partner_id": "summary-alpha",
                        "shipment_count": 1,
                    },
                    expected_event_count=5,
                    started_at=STARTED_AT,
                    completed_at=COMPLETED_AT,
                ),
                ExperimentRunModel(
                    id=EMPTY_TEST_RUN_ID,
                    scenario_name="not-started",
                    random_seed=7,
                    configuration={},
                    expected_event_count=3,
                    started_at=STARTED_AT,
                ),
                Partner(
                    id="summary-alpha",
                    name="Summary Alpha",
                    adapter_type="courier-alpha",
                ),
                Shipment(
                    tracking_number="SUMMARY-TRK-001",
                    current_status=ShipmentStatus.IN_TRANSIT,
                    current_status_occurred_at=STARTED_AT,
                ),
            ]
        )
        session.flush()

        database_events = [
            Event(
                partner_id="summary-alpha",
                partner_event_id=f"SUMMARY-EVENT-{event_number}",
                tracking_number="SUMMARY-TRK-001",
                status=ShipmentStatus.IN_TRANSIT,
                occurred_at=STARTED_AT + timedelta(seconds=event_number),
                received_at=STARTED_AT + timedelta(seconds=event_number + 1),
                raw_payload={"event_number": event_number},
                test_run_id=TEST_RUN_ID,
                processing_status=processing_status,
                state_applied=processing_status
                is EventProcessingStatus.PROCESSED,
            )
            for event_number, processing_status in enumerate(
                (
                    EventProcessingStatus.PROCESSED,
                    EventProcessingStatus.PROCESSED,
                    EventProcessingStatus.FAILED,
                    EventProcessingStatus.RECEIVED,
                ),
                start=1,
            )
        ]
        session.add_all(database_events)
        session.flush()

        session.add_all(
            [
                DeliveryAttempt(
                    event_id=database_event.id,
                    attempt_number=1,
                    result=attempt_result,
                    response_code=response_code,
                    latency_ms=10,
                    error=error,
                    started_at=STARTED_AT,
                    completed_at=STARTED_AT + timedelta(milliseconds=10),
                )
                for database_event, attempt_result, response_code, error in zip(
                    database_events[:3],
                    (
                        DeliveryAttemptResult.DELIVERED,
                        DeliveryAttemptResult.HTTP_ERROR,
                        DeliveryAttemptResult.TRANSPORT_ERROR,
                    ),
                    (202, 503, None),
                    (None, "downstream unavailable", "connection refused"),
                    strict=True,
                )
            ]
        )
        session.add_all(
            [
                DeliveryOutboxEntry(
                    event_id=database_events[0].id,
                    created_at=STARTED_AT,
                    published_at=STARTED_AT + timedelta(seconds=2),
                ),
                DeliveryOutboxEntry(
                    event_id=database_events[1].id,
                    created_at=STARTED_AT,
                ),
            ]
        )

    def override_session() -> Iterator[object]:
        with sessions() as session:
            yield session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    engine.dispose()


def test_summary_reports_run_definition_and_database_evidence(
    summary_client: TestClient,
) -> None:
    response = summary_client.get(f"/api/v1/test-runs/{TEST_RUN_ID}/summary")

    assert response.status_code == 200
    summary = response.json()
    assert summary == {
        "schema_version": 1,
        "test_run_id": str(TEST_RUN_ID),
        "scenario_name": "normal",
        "random_seed": 20260818,
        "configuration": {
            "partner_id": "summary-alpha",
            "shipment_count": 1,
        },
        "declared_event_count": 5,
        "started_at": STARTED_AT.replace(tzinfo=None).isoformat(),
        "completed_at": COMPLETED_AT.replace(tzinfo=None).isoformat(),
        "database_events": {
            "persisted": 4,
            "processed": 2,
            "failed": 1,
            "pending": 1,
        },
        "database_delivery_attempts": {
            "total": 3,
            "delivered": 1,
            "delivered_unique_events": 1,
            "http_error": 1,
            "transport_error": 1,
        },
        "database_outbox": {
            "durable": 2,
            "pending_publication": 1,
        },
    }


def test_summary_uses_zero_counts_when_a_run_has_no_database_evidence(
    summary_client: TestClient,
) -> None:
    response = summary_client.get(
        f"/api/v1/test-runs/{EMPTY_TEST_RUN_ID}/summary"
    )

    assert response.status_code == 200
    summary = response.json()
    assert summary["declared_event_count"] == 3
    assert summary["completed_at"] is None
    assert summary["database_events"] == {
        "persisted": 0,
        "processed": 0,
        "failed": 0,
        "pending": 0,
    }
    assert summary["database_delivery_attempts"] == {
        "total": 0,
        "delivered": 0,
        "delivered_unique_events": 0,
        "http_error": 0,
        "transport_error": 0,
    }
    assert summary["database_outbox"] == {
        "durable": 0,
        "pending_publication": 0,
    }


def test_summary_returns_not_found_for_an_unknown_test_run(
    summary_client: TestClient,
) -> None:
    response = summary_client.get(f"/api/v1/test-runs/{uuid4()}/summary")

    assert response.status_code == 404
    assert response.json() == {"detail": "Test run not found"}


def test_test_run_registration_and_completion_are_single_use(
    summary_client: TestClient,
) -> None:
    manifest = generate_input_manifest(
        seed=20260901,
        configuration=GeneratorConfiguration(
            partner_id="integration-alpha",
            shipment_count=1,
        ),
        test_run_id=UUID("00000000-0000-0000-0000-000000000707"),
    )

    registration = summary_client.post(
        "/api/v1/test-runs",
        json=manifest.model_dump(mode="json"),
    )
    duplicate = summary_client.post(
        "/api/v1/test-runs",
        json=manifest.model_dump(mode="json"),
    )
    completion = summary_client.post(
        f"/api/v1/test-runs/{manifest.test_run_id}/complete"
    )
    repeated_completion = summary_client.post(
        f"/api/v1/test-runs/{manifest.test_run_id}/complete"
    )

    assert registration.status_code == 201
    assert registration.json() == {
        "schema_version": 1,
        "test_run_id": str(manifest.test_run_id),
        "status": "registered",
    }
    assert duplicate.status_code == 409
    assert completion.status_code == 200
    assert completion.json()["status"] == "completed"
    assert repeated_completion.status_code == 409


def test_reconciliation_uses_private_simulator_evidence(
    summary_client: TestClient,
    monkeypatch,
) -> None:
    manifest = generate_input_manifest(
        seed=20260902,
        configuration=GeneratorConfiguration(
            partner_id="reconciliation-alpha",
            shipment_count=1,
        ),
        test_run_id=UUID("00000000-0000-0000-0000-000000000708"),
    )
    summary_client.post(
        "/api/v1/test-runs",
        json=manifest.model_dump(mode="json"),
    ).raise_for_status()
    expected = ReconciliationReport(
        test_run_id=manifest.test_run_id,
        generated=5,
        accepted=5,
        rejected=0,
        unique=5,
        processed=5,
        failed=0,
        pending=0,
        unaccounted=0,
        simulator_receipts=5,
        simulator_unique_events=5,
        duplicate_business_effects=0,
        incorrect_final_shipment_states=0,
        invariants_passed=True,
    )
    calls = []

    monkeypatch.setattr(
        trackrelay_main,
        "fetch_simulator_receipts",
        lambda downstream_url, **kwargs: calls.append(
            (downstream_url, kwargs["test_run_id"])
        )
        or (),
    )
    monkeypatch.setattr(
        trackrelay_main,
        "reconcile_manifest",
        lambda observed_manifest, **_kwargs: expected
        if observed_manifest == manifest
        else None,
    )

    response = summary_client.post(
        f"/api/v1/test-runs/{manifest.test_run_id}/reconciliation",
        json=manifest.model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
    assert calls == [(trackrelay_main.settings.downstream_url, manifest.test_run_id)]


def test_reconciliation_rejects_a_different_path_identity(
    summary_client: TestClient,
) -> None:
    manifest = generate_input_manifest(
        seed=20260903,
        configuration=GeneratorConfiguration(
            partner_id="mismatch-alpha",
            shipment_count=1,
        ),
    )

    response = summary_client.post(
        f"/api/v1/test-runs/{uuid4()}/reconciliation",
        json=manifest.model_dump(mode="json"),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Manifest and path test-run IDs differ"}
