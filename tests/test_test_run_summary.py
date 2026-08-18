"""Tests for database-backed experiment-run summaries."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pytest import fixture

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
from trackrelay.main import app
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
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
            "http_error": 1,
            "transport_error": 1,
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
        "http_error": 0,
        "transport_error": 0,
    }


def test_summary_returns_not_found_for_an_unknown_test_run(
    summary_client: TestClient,
) -> None:
    response = summary_client.get(f"/api/v1/test-runs/{uuid4()}/summary")

    assert response.status_code == 404
    assert response.json() == {"detail": "Test run not found"}
