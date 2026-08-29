"""Tests for the private synchronous-rehost experiment helper."""

from base64 import b64decode
from datetime import UTC, datetime, timedelta
from gzip import decompress
from uuid import UUID

import httpx
from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    ShipmentStatus,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    EVIDENCE_PREFIX,
    RESET_EVIDENCE_PREFIX,
    RUNTIME_EVIDENCE_PREFIX,
    RehostExperimentResetEvidence,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
    collect_downstream_delivery_intervals,
    encoded_evidence,
    encoded_reset_evidence,
    encoded_runtime_evidence,
    prepare_rehost_workload_point,
    reset_rehost_experiment_state,
    sample_rehost_runtime_timeline,
)
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.models import TestRun as ExperimentRunModel

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000901")


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def test_point_regenerates_the_frozen_healthy_manifest() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=2,
        duration_seconds=3,
        partner_id="load-alpha",
    )

    manifest = point.manifest()

    assert manifest.test_run_id == TEST_RUN_ID
    assert manifest.scenario_name == "healthy-baseline"
    assert manifest.events_generated == 6
    assert len(manifest.expected_final_shipments) == 6


def test_prepare_creates_private_run_state_and_sets_healthy_mode() -> None:
    engine, sessions = create_test_database()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"mode": "HEALTHY", "delay_seconds": 0},
        )

    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=2,
        duration_seconds=3,
        partner_id="load-alpha",
    )
    with httpx.Client(
        base_url="http://downstream:8001",
        transport=httpx.MockTransport(respond),
    ) as client:
        prepare_rehost_workload_point(
            point,
            sessions=sessions,
            downstream_client=client,
        )

    assert requests[0].url.path == "/control/mode"
    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        test_run = session.get(ExperimentRunModel, TEST_RUN_ID)
        assert partner is not None
        assert partner.adapter_type == "courier-alpha"
        assert test_run is not None
        assert test_run.expected_event_count == 6
    engine.dispose()


def test_reset_clears_only_synthetic_treatment_state() -> None:
    engine, sessions = create_test_database()
    started_at = datetime(2026, 8, 29, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Load Alpha",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )
        session.add(
            ExperimentRunModel(
                id=TEST_RUN_ID,
                scenario_name="reset-test",
                random_seed=1,
                configuration={},
                expected_event_count=1,
                started_at=started_at,
            )
        )
        session.add(
            Shipment(
                tracking_number="TRK-RESET",
                current_status=ShipmentStatus.CREATED,
                current_status_occurred_at=started_at,
            )
        )
        session.flush()
        event = Event(
            partner_id="load-alpha",
            partner_event_id="EVT-RESET",
            tracking_number="TRK-RESET",
            status=ShipmentStatus.CREATED,
            occurred_at=started_at,
            received_at=started_at,
            raw_payload={},
            test_run_id=TEST_RUN_ID,
            processing_status=EventProcessingStatus.PROCESSED,
            state_applied=True,
        )
        session.add(event)
        session.flush()
        session.add(
            DeliveryAttempt(
                event_id=event.id,
                attempt_number=1,
                result=DeliveryAttemptResult.DELIVERED,
                response_code=202,
                latency_ms=10,
                started_at=started_at,
                completed_at=started_at,
            )
        )

    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(
                200,
                json={"mode": "HEALTHY", "delay_seconds": 0},
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"cleared_event_count": 1})
        return httpx.Response(200, json=[])

    with httpx.Client(
        base_url="http://downstream:8001",
        transport=httpx.MockTransport(respond),
    ) as client:
        evidence = reset_rehost_experiment_state(
            sessions=sessions,
            downstream_client=client,
            now=lambda: started_at + timedelta(minutes=1),
        )

    assert evidence.database_rows_removed.model_dump() == {
        "delivery_attempts": 1,
        "events": 1,
        "shipments": 1,
        "test_runs": 1,
    }
    assert evidence.database_rows_remaining.model_dump() == {
        "delivery_attempts": 0,
        "events": 0,
        "shipments": 0,
        "test_runs": 0,
    }
    assert evidence.downstream_receipts_removed == 1
    assert requests == [
        ("PUT", "/control/mode"),
        ("DELETE", "/control/events"),
        ("GET", "/events"),
    ]
    with sessions() as session:
        assert session.get(Partner, "load-alpha") is not None
    encoded = encoded_reset_evidence(evidence)
    decoded = b64decode(encoded.removeprefix(RESET_EVIDENCE_PREFIX)).decode()
    assert RehostExperimentResetEvidence.model_validate_json(decoded) == evidence
    engine.dispose()


def test_evidence_line_round_trips_one_compact_json_object() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=1,
        duration_seconds=1,
        partner_id="load-alpha",
    )
    evidence = RehostServerEvidence(
        point=point,
        reconciliation=ReconciliationReport(
            test_run_id=TEST_RUN_ID,
            generated=1,
            accepted=1,
            rejected=0,
            unique=1,
            processed=1,
            failed=0,
            pending=0,
            unaccounted=0,
            simulator_receipts=1,
            simulator_unique_events=1,
        ),
    )

    line = encoded_evidence(evidence)
    decoded = b64decode(line.removeprefix(EVIDENCE_PREFIX)).decode("utf-8")

    assert RehostServerEvidence.model_validate_json(decoded) == evidence


def runtime_response(*, process_id: int, database_pool: object) -> dict:
    return {
        "schema_version": 2,
        "captured_at": "2026-08-29T00:00:00Z",
        "process_id": process_id,
        "process_cpu_seconds": 1,
        "process_max_rss_bytes": 1024,
        "python_thread_count": 2,
        "logical_cpu_count_available": 2,
        "gil_enabled": True,
        "host_logical_cpu_times": [],
        "host_memory_total_bytes": 2048,
        "host_memory_available_bytes": 1024,
        "database_pool": database_pool,
    }


def test_runtime_timeline_samples_both_private_processes_and_compresses() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=1,
        duration_seconds=10,
        partner_id="load-alpha",
    )
    sleeps: list[float] = []
    elapsed = [0.0]
    ready_samples: list[str] = []

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        elapsed[0] += seconds
    api_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=runtime_response(
                process_id=7,
                database_pool={
                    "checked_out": 1,
                    "checked_in": 4,
                    "pool_size": 5,
                    "overflow": 0,
                    "max_overflow": 10,
                },
            ),
        )
    )
    downstream_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=runtime_response(process_id=8, database_pool=None),
        )
    )
    with (
        httpx.Client(
            base_url="http://api:8000",
            transport=api_transport,
        ) as api_client,
        httpx.Client(
            base_url="http://downstream:8001",
            transport=downstream_transport,
        ) as downstream_client,
    ):
        timeline = sample_rehost_runtime_timeline(
            point,
            api_client=api_client,
            downstream_client=downstream_client,
            sleeper=sleep_and_advance,
            monotonic_clock=lambda: elapsed[0],
            on_ready=ready_samples.append,
            stop_requested=lambda: len(sleeps) == 2,
        )

    assert len(timeline.samples) == 3
    assert timeline.sampling_timeout_seconds == 10
    assert sleeps == [5, 5]
    assert ready_samples == [timeline.coverage_started_at]
    assert {sample.api.process_id for sample in timeline.samples} == {7}
    assert {sample.downstream.process_id for sample in timeline.samples} == {8}
    line = encoded_runtime_evidence(timeline)
    decoded = decompress(
        b64decode(line.removeprefix(RUNTIME_EVIDENCE_PREFIX))
    ).decode("utf-8")
    assert RehostRuntimeTimeline.model_validate_json(decoded) == timeline

    legacy = timeline.model_dump(mode="json")
    legacy["schema_version"] = 3
    legacy["sampling_duration_seconds"] = legacy.pop(
        "sampling_timeout_seconds"
    )
    migrated = RehostRuntimeTimeline.model_validate(legacy)
    assert migrated.schema_version == 3
    assert migrated.sampling_timeout_seconds == 10


def test_runtime_timeline_records_api_timeout_and_keeps_sampling_downstream() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=500,
        duration_seconds=10,
        partner_id="load-alpha",
    )
    api_request_count = 0
    sleeps: list[float] = []
    elapsed = [0.0]

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        elapsed[0] += seconds

    def api_response(request: httpx.Request) -> httpx.Response:
        nonlocal api_request_count
        api_request_count += 1
        if api_request_count == 2:
            raise httpx.ReadTimeout("overloaded", request=request)
        return httpx.Response(
            200,
            json=runtime_response(process_id=7, database_pool=None),
        )

    with (
        httpx.Client(
            base_url="http://api:8000",
            transport=httpx.MockTransport(api_response),
        ) as api_client,
        httpx.Client(
            base_url="http://downstream:8001",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=runtime_response(process_id=8, database_pool=None),
                )
            ),
        ) as downstream_client,
    ):
        timeline = sample_rehost_runtime_timeline(
            point,
            api_client=api_client,
            downstream_client=downstream_client,
            sleeper=sleep_and_advance,
            monotonic_clock=lambda: elapsed[0],
            stop_requested=lambda: len(sleeps) == 2,
        )

    failed_observation = timeline.samples[1]
    assert failed_observation.api is None
    assert failed_observation.downstream is not None
    assert failed_observation.failures[0].model_dump() == {
        "target": "api",
        "kind": "timeout",
        "status_code": None,
    }
    assert len(timeline.api_samples) == 2
    assert len(timeline.downstream_samples) == 3
    assert timeline.sampling_failures == failed_observation.failures
    encoded = encoded_runtime_evidence(timeline)
    decoded = decompress(
        b64decode(encoded.removeprefix(RUNTIME_EVIDENCE_PREFIX))
    ).decode("utf-8")
    assert RehostRuntimeTimeline.model_validate_json(decoded) == timeline


def test_runtime_timeline_requires_complete_read_before_signalling_ready() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=10,
        duration_seconds=5,
        partner_id="load-alpha",
    )
    api_request_count = 0
    sleeps: list[float] = []
    elapsed = [0.0]
    ready_samples: list[datetime] = []

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        elapsed[0] += seconds

    def api_response(request: httpx.Request) -> httpx.Response:
        nonlocal api_request_count
        api_request_count += 1
        if api_request_count == 1:
            raise httpx.ReadTimeout("not ready", request=request)
        return httpx.Response(
            200,
            json=runtime_response(process_id=7, database_pool=None),
        )

    with (
        httpx.Client(
            base_url="http://api:8000",
            transport=httpx.MockTransport(api_response),
        ) as api_client,
        httpx.Client(
            base_url="http://downstream:8001",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=runtime_response(process_id=8, database_pool=None),
                )
            ),
        ) as downstream_client,
    ):
        timeline = sample_rehost_runtime_timeline(
            point,
            api_client=api_client,
            downstream_client=downstream_client,
            sleeper=sleep_and_advance,
            monotonic_clock=lambda: elapsed[0],
            on_ready=ready_samples.append,
            stop_requested=lambda: 5 in sleeps,
        )

    assert len(timeline.samples) == 3
    assert timeline.samples[0].api is None
    assert timeline.samples[1].complete
    assert ready_samples == [timeline.coverage_started_at]
    assert sleeps == [1, 5]


def test_runtime_timeline_fails_closed_at_the_absolute_timeout() -> None:
    point = RehostWorkloadPoint(
        test_run_id=TEST_RUN_ID,
        request_rate_per_second=10,
        duration_seconds=5,
        partner_id="load-alpha",
    )
    elapsed = [0.0]

    def sleep_and_advance(seconds: float) -> None:
        elapsed[0] += seconds

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=runtime_response(process_id=7, database_pool=None),
        )
    )
    with (
        httpx.Client(
            base_url="http://api:8000",
            transport=transport,
        ) as api_client,
        httpx.Client(
            base_url="http://downstream:8001",
            transport=transport,
        ) as downstream_client,
        raises(RuntimeError, match="did not receive the stop signal"),
    ):
        sample_rehost_runtime_timeline(
            point,
            api_client=api_client,
            downstream_client=downstream_client,
            sampling_timeout_seconds=5,
            sleeper=sleep_and_advance,
            monotonic_clock=lambda: elapsed[0],
        )


def test_delivery_attempts_are_aggregated_into_aligned_intervals() -> None:
    engine, sessions = create_test_database()
    started_at = datetime(2026, 8, 29, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Load Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            ExperimentRunModel(
                id=TEST_RUN_ID,
                scenario_name="healthy-baseline",
                random_seed=20260806,
                configuration={},
                expected_event_count=3,
                started_at=started_at,
            )
        )
        for index, (offset, result, latency) in enumerate(
            (
                (1, DeliveryAttemptResult.DELIVERED, 10),
                (4, DeliveryAttemptResult.HTTP_ERROR, 30),
                (6, DeliveryAttemptResult.TRANSPORT_ERROR, 50),
            ),
            start=1,
        ):
            tracking_number = f"TRK-{index}"
            session.add(
                Shipment(
                    tracking_number=tracking_number,
                    current_status=ShipmentStatus.CREATED,
                    current_status_occurred_at=started_at,
                )
            )
            session.flush()
            event = Event(
                partner_id="load-alpha",
                partner_event_id=f"EVT-{index}",
                tracking_number=tracking_number,
                status=ShipmentStatus.CREATED,
                occurred_at=started_at,
                received_at=started_at,
                raw_payload={},
                test_run_id=TEST_RUN_ID,
                processing_status=EventProcessingStatus.PROCESSED,
                state_applied=True,
            )
            session.add(event)
            session.flush()
            attempt_started_at = started_at + timedelta(seconds=offset)
            session.add(
                DeliveryAttempt(
                    event_id=event.id,
                    attempt_number=1,
                    result=result,
                    response_code=202 if result is DeliveryAttemptResult.DELIVERED else None,
                    latency_ms=latency,
                    error=None if result is DeliveryAttemptResult.DELIVERED else "test",
                    started_at=attempt_started_at,
                    completed_at=attempt_started_at
                    + timedelta(milliseconds=latency),
                )
            )

    intervals = collect_downstream_delivery_intervals(
        TEST_RUN_ID,
        sessions=sessions,
    )

    assert len(intervals) == 2
    assert intervals[0].attempt_count == 2
    assert intervals[0].delivered_count == 1
    assert intervals[0].http_error_count == 1
    assert intervals[0].p95_latency_ms == 30
    assert intervals[1].transport_error_count == 1
    assert intervals[1].maximum_latency_ms == 50
    engine.dispose()
