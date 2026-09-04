"""Tests for the failure-safe Stage 9.5 integration runner."""

from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import httpx
from pytest import raises

from trackrelay.aws_async_integration import (
    ASYNC_INTEGRATION_DEFINITION,
    AsyncCloudWatchEvidence,
    AsyncCloudWatchMetricEvidence,
    AsyncIntegrationPointResult,
    AsyncRequestObservation,
    AwsAsyncIntegrationCleanupError,
    AwsAsyncIntegrationError,
    build_exact_event_manifest,
    collect_async_cloudwatch_evidence,
    run_async_integration_point,
    run_async_integration_session,
)
from trackrelay.aws_session import AwsSession, load_manifest, write_manifest
from trackrelay.experiments.async_guardrails import (
    AsyncProcessingEvidence,
    AsyncProcessingObservation,
    evaluate_async_processing_guardrails,
)
from trackrelay.experiments.reconciliation import ReconciliationReport

SESSION_ID = "cloud-session-3-20260901T090000Z"
REVISION = "a" * 40
STARTED_AT = datetime(2026, 9, 1, 9, 10, tzinfo=UTC)


def ready_session(tmp_path: Path) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile="trackrelay-admin",
        region="ap-southeast-3",
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        deployment_mode="async",
    )
    session.evidence_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "3.00",
            "deployment_mode": "async",
            "git_revision": REVISION,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": session.rehost_instance_type,
            "session_id": session.session_id,
            "status": "async_deployed",
        },
    )
    return session


def completed(arguments, *, stdout="") -> CompletedProcess[str]:
    return CompletedProcess(arguments, 0, stdout=stdout, stderr="")


def local_runner(arguments, _input_text=None) -> CompletedProcess[str]:
    command = tuple(arguments)
    if command == ("git", "status", "--porcelain"):
        return completed(command)
    if command == ("git", "rev-parse", "HEAD"):
        return completed(command, stdout=f"{REVISION}\n")
    output_name = command[-1]
    outputs = {
        "async_api_url": "http://trackrelay.example",
        "delivery_queue_url": "https://sqs.ap-southeast-3.amazonaws.com/123/source",
        "delivery_dead_letter_queue_url": "https://sqs.ap-southeast-3.amazonaws.com/123/dlq",
    }
    return completed(command, stdout=f"{outputs[output_name]}\n")


def passing_point(event_count: int) -> AsyncIntegrationPointResult:
    test_run_id = UUID(int=event_count)
    reconciliation = ReconciliationReport(
        test_run_id=test_run_id,
        generated=event_count,
        accepted=event_count,
        rejected=0,
        unique=event_count,
        processed=event_count,
        failed=0,
        pending=0,
        unaccounted=0,
        simulator_receipts=event_count,
        simulator_unique_events=event_count,
        duplicate_business_effects=0,
        incorrect_final_shipment_states=0,
        invariants_passed=True,
    )
    observations = tuple(
        AsyncProcessingObservation(
            observed_at=STARTED_AT + timedelta(seconds=second),
            seconds_after_load_ended=second,
            accepted_unique_events=event_count,
            durable_outbox_entries=event_count,
            completed_delivery_events=event_count,
            pending_outbox_entries=0,
            source_queue_visible_messages=0,
            source_queue_in_flight_messages=0,
            source_queue_delayed_messages=0,
            dead_letter_queue_messages=0,
        )
        for second in range(0, 181, 10)
    )
    processing = evaluate_async_processing_guardrails(
        AsyncProcessingEvidence(
            test_run_id=test_run_id,
            reconciliation=reconciliation,
            observations=observations,
        )
    )
    return AsyncIntegrationPointResult(
        event_count=event_count,
        test_run_id=test_run_id,
        load_started_at=STARTED_AT,
        load_ended_at=STARTED_AT,
        request_observations=tuple(
            AsyncRequestObservation(
                sequence_number=index,
                status_code=201,
                latency_ms=10,
            )
            for index in range(1, event_count + 1)
        ),
        ingestion_p95_ms=10,
        ingestion_error_percent=0,
        processing=processing,
        guardrails_passed=True,
    )


def approval(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "3.00",
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def cloudwatch_evidence() -> AsyncCloudWatchEvidence:
    return AsyncCloudWatchEvidence(
        collected_at=STARTED_AT + timedelta(minutes=9),
        window_started_at=STARTED_AT,
        window_ended_at=STARTED_AT + timedelta(minutes=8),
        series=tuple(
            AsyncCloudWatchMetricEvidence(
                query_id=query_id,
                timestamps=(STARTED_AT,),
                values=(1,),
            )
            for query_id in (
                "alb_requests",
                "api_cpu",
                "queue_sent",
                "rds_cpu",
                "simulator_cpu",
                "worker_tasks",
            )
        ),
    )


def test_exact_manifest_builder_freezes_one_ten_and_one_hundred_events() -> None:
    for event_count in ASYNC_INTEGRATION_DEFINITION.event_counts:
        manifest = build_exact_event_manifest(
            event_count,
            test_run_id=UUID(int=event_count),
        )

        assert manifest.events_generated == event_count
        assert manifest.expected_unique_events == event_count
        assert len(manifest.expected_events) == event_count
        assert set(manifest.expected_final_shipments) == {
            event.tracking_number for event in manifest.expected_events
        }


def test_point_runner_observes_the_complete_stable_drain_window(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    current_time = STARTED_AT
    timer_value = 0.0
    observed_test_run_id: UUID | None = None

    def clock() -> datetime:
        return current_time

    def sleeper(seconds: float) -> None:
        nonlocal current_time
        current_time += timedelta(seconds=seconds)

    def timer() -> float:
        nonlocal timer_value
        timer_value += 0.01
        return timer_value

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_test_run_id
        if request.url.path == "/api/v1/test-runs":
            observed_test_run_id = UUID(loads(request.content)["test_run_id"])
            return httpx.Response(201, json={"status": "registered"})
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path.endswith("/summary"):
            return httpx.Response(
                200,
                json={
                    "test_run_id": str(observed_test_run_id),
                    "declared_event_count": 1,
                    "completed_at": STARTED_AT.isoformat(),
                    "database_events": {
                        "persisted": 1,
                        "processed": 1,
                        "failed": 0,
                        "pending": 0,
                    },
                    "database_delivery_attempts": {
                        "total": 1,
                        "delivered": 1,
                        "delivered_unique_events": 1,
                        "http_error": 0,
                        "transport_error": 0,
                    },
                    "database_outbox": {
                        "durable": 1,
                        "pending_publication": 0,
                    },
                },
            )
        if request.url.path.endswith("/reconciliation"):
            return httpx.Response(
                200,
                json=ReconciliationReport(
                    test_run_id=observed_test_run_id,
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
                    duplicate_business_effects=0,
                    incorrect_final_shipment_states=0,
                    invariants_passed=True,
                ).model_dump(mode="json"),
            )
        return httpx.Response(201, json={"delivery_status": "queued"})

    def aws_runner(arguments, _input_text=None):
        return completed(
            tuple(arguments),
            stdout=(
                '{"ApproximateNumberOfMessages":"0",'
                '"ApproximateNumberOfMessagesNotVisible":"0",'
                '"ApproximateNumberOfMessagesDelayed":"0"}'
            ),
        )

    with httpx.Client(
        base_url="http://trackrelay.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = run_async_integration_point(
            session,
            event_count=1,
            api_url="http://trackrelay.example",
            source_queue_url="https://sqs.example/source",
            dead_letter_queue_url="https://sqs.example/dlq",
            runner=aws_runner,
            now=clock,
            sleeper=sleeper,
            timer=timer,
            client=client,
        )

    assert result.guardrails_passed is True
    assert result.processing.drain_reached_after_seconds == 0
    assert result.processing.drain_confirmed_after_seconds == 180
    assert len(result.processing.evidence.observations) == 19
    assert (
        session.evidence_dir
        / "async-integration"
        / "events-1"
        / "processing-observations.json"
    ).is_file()


def test_complete_integration_runs_frozen_points_then_destroys_and_verifies(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    def point_runner(_session, *, event_count, **_kwargs):
        actions.append(f"events-{event_count}")
        return passing_point(event_count)

    summary = run_async_integration_session(
        session,
        **approval(session),
        point_runner=point_runner,
        metric_collector=lambda _session, **_kwargs: cloudwatch_evidence(),
        runner=local_runner,
        destroyer=lambda _session: actions.append("destroy"),
        teardown_verifier=lambda _session: actions.append("verify"),
        now=lambda: STARTED_AT + timedelta(minutes=10),
    )

    assert actions == ["events-1", "events-10", "events-100", "destroy", "verify"]
    assert summary.all_guardrails_passed is True
    assert (session.evidence_dir / "async-integration" / "summary.json").is_file()
    assert load_manifest(session)["status"] == "async_integration_passed"


def test_cloudwatch_collector_requires_dashboard_and_published_native_series(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    dimensions = {
        "scaling_metrics_namespace": "TrackRelay/Elasticity",
        "api_service_name": "trackrelay-12345678-api",
        "cluster_name": "trackrelay-12345678-async",
        "dashboard_name": "trackrelay-12345678-async",
        "dead_letter_queue_name": "trackrelay-12345678-delivery-dlq",
        "delivery_queue_name": "trackrelay-12345678-delivery",
        "load_balancer_dimension": "app/trackrelay/1234567890abcdef",
        "rds_identifier": "trackrelay-12345678-postgres",
        "simulator_service_name": "trackrelay-12345678-simulator",
        "worker_service_name": "trackrelay-12345678-worker",
    }
    observed_query_ids: set[str] = set()

    def runner(arguments, _input_text=None):
        command = tuple(arguments)
        if command[-1] == "async_observability_dimensions":
            return completed(command, stdout=dumps(dimensions))
        if "get-dashboard" in command:
            return completed(
                command,
                stdout=dumps(
                    {
                        "DashboardName": dimensions["dashboard_name"],
                        "DashboardBody": dumps({"widgets": [{"type": "metric"}] * 7}),
                    }
                ),
            )
        query_path = Path(
            command[command.index("--metric-data-queries") + 1].removeprefix("file://")
        )
        queries = loads(query_path.read_text(encoding="utf-8"))
        observed_query_ids.update(query["Id"] for query in queries)
        return completed(
            command,
            stdout=dumps(
                {
                    "MetricDataResults": [
                        {
                            "Id": query["Id"],
                            "StatusCode": "Complete",
                            "Timestamps": [STARTED_AT.isoformat()],
                            "Values": [1.0],
                        }
                        for query in queries
                    ]
                }
            ),
        )

    evidence = collect_async_cloudwatch_evidence(
        session,
        window_started_at=STARTED_AT,
        window_ended_at=STARTED_AT + timedelta(minutes=8),
        runner=runner,
        now=lambda: STARTED_AT + timedelta(minutes=9),
    )

    assert evidence.dashboard_widget_count == 7
    assert observed_query_ids == {
        "alb_requests",
        "api_cpu",
        "queue_sent",
        "rds_cpu",
        "simulator_cpu",
        "worker_tasks",
    }


def test_point_failure_still_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    def point_runner(_session, *, event_count, **_kwargs):
        actions.append(f"events-{event_count}")
        if event_count == 10:
            raise AwsAsyncIntegrationError("simulated guardrail failure")
        return passing_point(event_count)

    with raises(AwsAsyncIntegrationError, match="simulated guardrail failure"):
        run_async_integration_session(
            session,
            **approval(session),
            point_runner=point_runner,
            runner=local_runner,
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == ["events-1", "events-10", "destroy", "verify"]


def test_preparation_failure_after_approval_still_cleans_up(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    actions: list[str] = []

    def invalid_endpoint_runner(arguments, input_text=None):
        result = local_runner(arguments, input_text)
        if tuple(arguments)[-1] == "async_api_url":
            return completed(tuple(arguments), stdout="file:///invalid\n")
        return result

    with raises(AwsAsyncIntegrationError, match="invalid integration endpoints"):
        run_async_integration_session(
            session,
            **approval(session),
            runner=invalid_endpoint_runner,
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == ["destroy", "verify"]


def test_cleanup_failure_keeps_the_workflow_failure(tmp_path: Path) -> None:
    session = ready_session(tmp_path)

    def fail_point(_session, **_kwargs):
        raise AwsAsyncIntegrationError("workload failed")

    def fail_destroy(_session):
        raise RuntimeError("destroy failed")

    with raises(AwsAsyncIntegrationCleanupError) as captured:
        run_async_integration_session(
            session,
            **approval(session),
            point_runner=fail_point,
            runner=local_runner,
            destroyer=fail_destroy,
            teardown_verifier=lambda _session: None,
        )

    assert isinstance(captured.value.workflow_error, AwsAsyncIntegrationError)
    assert len(captured.value.cleanup_errors) == 1
