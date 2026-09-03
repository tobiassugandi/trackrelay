"""Tests for the guarded Stage 9.6 fixed-worker controller."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import httpx
from pytest import MonkeyPatch, mark, raises

from trackrelay.aws_elasticity_cloudwatch import (
    ElasticityCloudWatchDatapoint,
    ElasticityCloudWatchEvidence,
    ElasticityCloudWatchMetricSeries,
    metric_definitions,
)
from trackrelay.aws_fixed_control import (
    AwsFixedControlError,
    AwsFixedControlQualificationError,
    FixedControlIngestionStepResult,
    FixedControlObservation,
    FixedControlResult,
    collect_fixed_control_observation,
    derive_ingestion_steps,
    evaluate_fixed_control_qualification,
    execute_elasticity_workload,
    execute_fixed_control,
    run_fixed_control_session,
    validate_fixed_control_approval,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.operator_status import progress_output
from trackrelay.runtime_metrics import DatabasePoolMetrics, RuntimeMetricsSnapshot

SESSION_ID = "cloud-session-4-20260902T090000Z"
GIT_REVISION = "d" * 40
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000946")
API_URL = "http://trackrelay-fixed.example.com"
SOURCE_QUEUE_URL = (
    "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-delivery"
)
DLQ_URL = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-dlq"
CLUSTER_NAME = "trackrelay-8a7e37db-async"
WORKER_SERVICE = "trackrelay-8a7e37db-worker"


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
    returncode: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, returncode, stdout, "")


def ready_session(tmp_path: Path, *, status: str = "async_deployed") -> AwsSession:
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
            "approved_cost_ceiling_usd": "5.00",
            "deployment_mode": "async",
            "git_revision": GIT_REVISION,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": session.rehost_instance_type,
            "session_id": session.session_id,
            "status": status,
        },
    )
    return session


def approvals(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "5.0",
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def observation(
    *,
    observed_at: datetime,
    step_name: str,
    rate: int,
    queue_work: int,
    persisted: int,
    delivered: int,
) -> FixedControlObservation:
    runtime = RuntimeMetricsSnapshot(
        captured_at=observed_at,
        process_id=7,
        process_cpu_seconds=1,
        process_max_rss_bytes=1,
        python_thread_count=1,
        logical_cpu_count_available=1,
        gil_enabled=True,
        host_logical_cpu_times=(),
        host_memory_total_bytes=None,
        host_memory_available_bytes=None,
        database_pool=DatabasePoolMetrics(
            checked_out=2,
            checked_in=3,
            pool_size=5,
            overflow=0,
            max_overflow=10,
        ),
    )
    return FixedControlObservation(
        observed_at=observed_at,
        seconds_after_load_started=max(
            0,
            (observed_at - datetime(2026, 9, 2, tzinfo=UTC)).total_seconds(),
        ),
        phase="post-load" if step_name == "post-load" else "during-load",
        step_name=step_name,
        offered_rate_per_second=rate,
        database_persisted_events=persisted,
        database_processed_events=persisted,
        database_failed_events=0,
        database_pending_events=0,
        completed_delivery_events=delivered,
        durable_outbox_entries=persisted,
        pending_outbox_entries=0,
        source_queue_visible_messages=queue_work,
        source_queue_in_flight_messages=0,
        source_queue_delayed_messages=0,
        dead_letter_queue_messages=0,
        worker_desired_count=1,
        worker_running_count=1,
        worker_pending_count=0,
        api_runtime=runtime,
    )


def passing_result() -> FixedControlResult:
    definition = ELASTICITY_WORKLOAD_DEFINITION
    started_at = datetime(2026, 9, 2, tzinfo=UTC)
    ended_at = started_at + timedelta(seconds=definition.duration_seconds)
    steps = tuple(
        FixedControlIngestionStepResult(
            step_name=step.name,
            offered_rate_per_second=step.offered_rate_per_second,
            duration_seconds=step.duration_seconds,
            expected_request_count=step.expected_request_count,
            observed_request_count=step.expected_request_count,
            p95_response_latency_ms=100,
            request_error_percent=0,
        )
        for step in definition.steps
    )
    observations = tuple(
        observation(
            observed_at=started_at + timedelta(seconds=30 + 60 * index),
            step_name=step.name,
            rate=step.offered_rate_per_second,
            queue_work=20 if step.name == "peak-25" else 0,
            persisted=100 * (index + 1),
            delivered=100 * (index + 1) - (20 if step.name == "peak-25" else 0),
        )
        for index, step in enumerate(definition.steps)
    ) + (
        observation(
            observed_at=ended_at + timedelta(seconds=190),
            step_name="post-load",
            rate=0,
            queue_work=0,
            persisted=definition.expected_request_count,
            delivered=definition.expected_request_count,
        ),
    )
    count = definition.expected_request_count
    return FixedControlResult(
        test_run_id=TEST_RUN_ID,
        definition=definition,
        load_started_at=started_at,
        load_ended_at=ended_at,
        k6_exit_code=0,
        dropped_iteration_count=0,
        ingestion_steps=steps,
        observations=observations,
        observation_failures=(),
        drain_stability_confirmed=True,
        reconciliation=ReconciliationReport(
            test_run_id=TEST_RUN_ID,
            generated=count,
            accepted=count,
            rejected=0,
            unique=count,
            processed=count,
            failed=0,
            pending=0,
            unaccounted=0,
            simulator_receipts=count,
            simulator_unique_events=count,
            duplicate_business_effects=0,
            incorrect_final_shipment_states=0,
            invariants_passed=True,
        ),
    )


def cloudwatch_evidence(
    test_run_id: UUID = TEST_RUN_ID,
) -> ElasticityCloudWatchEvidence:
    started_at = datetime(2026, 9, 2, tzinfo=UTC)
    ended_at = started_at + timedelta(
        seconds=ELASTICITY_WORKLOAD_DEFINITION.duration_seconds
    )
    values = {
        "alb_requests": 400,
        "alb_p95_latency": 0.1,
        "api_cpu": 20,
        "api_memory": 20,
        "worker_running_tasks": 1,
        "worker_cpu": 90,
        "simulator_cpu": 20,
        "simulator_memory": 20,
        "rds_cpu": 20,
        "rds_connections": 10,
        "rds_freeable_memory": 256 * 1024 * 1024,
        "rds_read_latency": 0.005,
        "rds_write_latency": 0.005,
        "rds_read_iops": 10,
        "rds_write_iops": 10,
    }
    timestamps = tuple(started_at + timedelta(minutes=minute) for minute in range(11))
    return ElasticityCloudWatchEvidence(
        test_run_id=test_run_id,
        window_started_at=started_at,
        window_ended_at=ended_at,
        collected_at=ended_at + timedelta(minutes=5),
        series=tuple(
            ElasticityCloudWatchMetricSeries(
                query_id=definition.query_id,
                namespace=definition.namespace,
                metric_name=definition.metric_name,
                statistic=definition.statistic,
                unit=definition.unit,
                datapoints=tuple(
                    ElasticityCloudWatchDatapoint(
                        interval_started_at=timestamp,
                        value=values.get(definition.query_id, 0),
                    )
                    for timestamp in timestamps
                ),
            )
            for definition in metric_definitions()
        ),
    )


def terraform_output_name(arguments: Sequence[str]) -> str | None:
    call = tuple(arguments)
    if len(call) >= 5 and call[2] == "output":
        return call[4]
    return None


def controller_runner(arguments: Sequence[str], _input: str | None):
    call = tuple(arguments)
    if call == ("git", "status", "--porcelain"):
        return completed(call)
    if call == ("git", "rev-parse", "HEAD"):
        return completed(call, stdout=GIT_REVISION)
    output_name = terraform_output_name(call)
    outputs = {
        "async_api_url": API_URL,
        "delivery_queue_url": SOURCE_QUEUE_URL,
        "delivery_dead_letter_queue_url": DLQ_URL,
        "async_observability_dimensions": dumps(
            {
                "api_service_name": "trackrelay-8a7e37db-api",
                "cluster_name": CLUSTER_NAME,
                "dashboard_name": "trackrelay-8a7e37db-async",
                "dead_letter_queue_name": "trackrelay-8a7e37db-delivery-dlq",
                "delivery_queue_name": "trackrelay-8a7e37db-delivery",
                "load_balancer_dimension": "app/example/123",
                "rds_identifier": "trackrelay-example",
                "simulator_service_name": "trackrelay-8a7e37db-simulator",
                "worker_service_name": WORKER_SERVICE,
            }
        ),
        "async_service_capacity": dumps(
            {
                "api": {
                    "cpu_units": 1024,
                    "desired_count": 2,
                    "memory_mib": 2048,
                    "service_name": "trackrelay-8a7e37db-api",
                },
                "simulator": {
                    "cpu_units": 256,
                    "desired_count": 1,
                    "memory_mib": 512,
                    "service_name": "trackrelay-8a7e37db-simulator",
                },
                "worker": {
                    "cpu_units": 256,
                    "desired_count": 1,
                    "memory_mib": 512,
                    "service_name": WORKER_SERVICE,
                },
            }
        ),
    }
    if output_name is not None:
        return completed(call, stdout=outputs[output_name])
    raise AssertionError(call)


def test_fixed_control_requires_the_exact_deployed_session_4(tmp_path: Path) -> None:
    session = ready_session(tmp_path)

    manifest = validate_fixed_control_approval(session, **approvals(session))

    assert manifest["status"] == "async_deployed"
    with raises(AwsSessionError, match="approved session ID"):
        validate_fixed_control_approval(
            session,
            **{**approvals(session), "approved_session_id": "wrong"},
        )


def test_ingestion_derivation_requires_every_tagged_step() -> None:
    metrics: dict[str, object] = {"dropped_iterations": {"values": {"count": 0}}}
    for step in ELASTICITY_WORKLOAD_DEFINITION.steps:
        tag = f"{{step:{step.name}}}"
        metrics[f"http_reqs{tag}"] = {"values": {"count": step.expected_request_count}}
        metrics[f"http_req_duration{tag}"] = {"values": {"p(95)": 120}}
        metrics[f"http_req_failed{tag}"] = {"values": {"rate": 0}}

    results = derive_ingestion_steps(
        ELASTICITY_WORKLOAD_DEFINITION,
        {"metrics": metrics},
    )

    assert all(result.ingestion_guardrails_passed for result in results)
    del metrics["http_reqs{step:peak-25}"]
    with raises(AwsFixedControlError, match="peak-25"):
        derive_ingestion_steps(
            ELASTICITY_WORKLOAD_DEFINITION,
            {"metrics": metrics},
        )


def test_live_observation_aligns_database_queue_and_fixed_worker(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    summary = {
        "test_run_id": str(TEST_RUN_ID),
        "declared_event_count": 10,
        "completed_at": None,
        "database_events": {
            "persisted": 10,
            "processed": 10,
            "failed": 0,
            "pending": 0,
        },
        "database_delivery_attempts": {
            "total": 7,
            "delivered": 7,
            "delivered_unique_events": 7,
            "http_error": 0,
            "transport_error": 0,
        },
        "database_outbox": {"durable": 10, "pending_publication": 0},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/summary"):
            return httpx.Response(200, json=summary, request=request)
        return httpx.Response(503, request=request)

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        if "get-queue-attributes" in call:
            queue_url = call[call.index("--queue-url") + 1]
            attributes = (
                {"ApproximateNumberOfMessages": "0"}
                if queue_url == DLQ_URL
                else {
                    "ApproximateNumberOfMessages": "2",
                    "ApproximateNumberOfMessagesNotVisible": "1",
                    "ApproximateNumberOfMessagesDelayed": "0",
                }
            )
            return completed(call, stdout=dumps(attributes))
        if "describe-services" in call:
            return completed(call, stdout="[1, 1, 0]")
        raise AssertionError(call)

    started_at = datetime(2026, 9, 2, tzinfo=UTC)
    with httpx.Client(
        base_url=API_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        observed = collect_fixed_control_observation(
            session,
            api_client=client,
            test_run_id=TEST_RUN_ID,
            source_queue_url=SOURCE_QUEUE_URL,
            dead_letter_queue_url=DLQ_URL,
            cluster_name=CLUSTER_NAME,
            worker_service_name=WORKER_SERVICE,
            load_started_at=started_at,
            observed_at=started_at + timedelta(seconds=185),
            load_ended_at=None,
            definition=ELASTICITY_WORKLOAD_DEFINITION,
            runner=runner,
        )

    assert observed.step_name == "peak-25"
    assert observed.offered_rate_per_second == 25
    assert observed.source_queue_work == 3
    assert observed.worker_running_count == 1
    assert observed.api_runtime is None
    assert not observed.processing_drained


@mark.parametrize("treatment", tuple(ElasticityTreatment))
def test_execution_uses_definition_and_confirms_stable_drain(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    treatment: ElasticityTreatment,
) -> None:
    session = ready_session(tmp_path)
    definition = ELASTICITY_WORKLOAD_DEFINITION
    count = definition.expected_request_count
    evidence_root = tmp_path / "fixed"
    evidence_root.mkdir()
    metrics: dict[str, object] = {"dropped_iterations": {"values": {"count": 0}}}
    for step in definition.steps:
        tag = f"{{step:{step.name}}}"
        metrics[f"http_reqs{tag}"] = {"values": {"count": step.expected_request_count}}
        metrics[f"http_req_duration{tag}"] = {"values": {"p(95)": 100}}
        metrics[f"http_req_failed{tag}"] = {"values": {"rate": 0}}
    (evidence_root / "k6-summary.json").write_text(
        dumps({"metrics": metrics}),
        encoding="utf-8",
    )

    class CompletedK6:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def wait(self, *, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr("trackrelay.aws_fixed_control.Popen", CompletedK6)
    registered: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/test-runs":
            registered.append(loads(request.content))
            return httpx.Response(201, json={}, request=request)
        if request.url.path.endswith("/summary"):
            return httpx.Response(
                200,
                json={
                    "test_run_id": registered[0]["test_run_id"],
                    "declared_event_count": count,
                    "completed_at": None,
                    "database_events": {
                        "persisted": count,
                        "processed": count,
                        "failed": 0,
                        "pending": 0,
                    },
                    "database_delivery_attempts": {
                        "total": count,
                        "delivered": count,
                        "delivered_unique_events": count,
                        "http_error": 0,
                        "transport_error": 0,
                    },
                    "database_outbox": {
                        "durable": count,
                        "pending_publication": 0,
                    },
                },
                request=request,
            )
        if request.url.path.endswith("/complete"):
            return httpx.Response(204, request=request)
        if request.url.path.endswith("/reconciliation"):
            return httpx.Response(
                200,
                json={
                    "test_run_id": registered[0]["test_run_id"],
                    "generated": count,
                    "accepted": count,
                    "rejected": 0,
                    "unique": count,
                    "processed": count,
                    "failed": 0,
                    "pending": 0,
                    "unaccounted": 0,
                    "simulator_receipts": count,
                    "simulator_unique_events": count,
                    "duplicate_business_effects": 0,
                    "incorrect_final_shipment_states": 0,
                    "invariants_passed": True,
                },
                request=request,
            )
        return httpx.Response(503, request=request)

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        if "get-queue-attributes" in call:
            names = call[call.index("--attribute-names") + 1 : call.index("--query")]
            return completed(call, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in call:
            return completed(call, stdout="[1, 1, 0]")
        raise AssertionError(call)

    started_at = datetime(2026, 9, 2, tzinfo=UTC)
    timestamps = iter(
        started_at + timedelta(seconds=seconds)
        for seconds in (0, 60, 120, 180, 240, 300, 360, 420, 480)
    )
    messages = []
    with (
        progress_output(messages.append, repeat_interval_seconds=0),
        httpx.Client(
            base_url=API_URL,
            transport=httpx.MockTransport(handler),
        ) as client,
    ):
        execute = (
            execute_fixed_control
            if treatment is ElasticityTreatment.FIXED
            else execute_elasticity_workload
        )
        treatment_arguments = (
            {} if treatment is ElasticityTreatment.FIXED else {"treatment": treatment}
        )
        result = execute(
            session,
            **treatment_arguments,
            api_url=API_URL,
            source_queue_url=SOURCE_QUEUE_URL,
            dead_letter_queue_url=DLQ_URL,
            cluster_name=CLUSTER_NAME,
            worker_service_name=WORKER_SERVICE,
            evidence_root=evidence_root,
            runner=runner,
            now=lambda: next(timestamps),
            sleeper=lambda _seconds: None,
            api_client=client,
        )

    assert registered[0]["events_generated"] == count
    assert registered[0]["scenario_name"] == (
        "elasticity-fixed-control"
        if treatment is ElasticityTreatment.FIXED
        else "elasticity-elastic-treatment"
    )
    assert result.drain_stability_confirmed is True
    assert result.measurement_complete is True
    assert result.worker_pressure_observed is False
    assert (evidence_root / "result.json").is_file()
    label = "Fixed" if treatment is ElasticityTreatment.FIXED else "Elastic"
    assert any(
        message.startswith(f"{label} workload:")
        and "workers=1/1" in message
        and "queue=0" in message
        for message in messages
    )
    assert any(
        message.startswith(f"{label} drain:") and "stable-empty=" in message
        for message in messages
    )
    assert f"{label}: stable drain confirmed" in messages
    assert any(message.startswith(f"{label} reconciliation:") for message in messages)


def test_fixed_control_success_leaves_stack_for_reset(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    cleanup = []
    expected = passing_result()

    def treatment_runner(_session, *, evidence_root: Path, **_kwargs):
        (evidence_root / "result.json").write_text("{}\n", encoding="utf-8")
        return expected

    result = run_fixed_control_session(
        session,
        **approvals(session),
        treatment_runner=treatment_runner,
        metric_collector=lambda _session, **_kwargs: cloudwatch_evidence(),
        runner=controller_runner,
        destroyer=lambda _session: cleanup.append("destroy"),
        teardown_verifier=lambda _session: cleanup.append("verify"),
    )

    assert result.measurement == expected
    assert result.qualification.qualified is True
    assert cleanup == []
    manifest = load_manifest(session)
    assert manifest["status"] == "fixed_control_qualified"
    assert manifest["fixed_control"]["measurement_complete"] is True
    assert manifest["fixed_control"]["worker_pressure_observed"] is True


def test_fixed_control_qualification_rejects_non_worker_saturation() -> None:
    result = passing_result()
    evidence = cloudwatch_evidence()
    saturated_series = tuple(
        series.model_copy(
            update={
                "datapoints": tuple(
                    point.model_copy(update={"value": 80})
                    for point in series.datapoints
                )
            }
        )
        if series.query_id == "api_cpu"
        else series
        for series in evidence.series
    )

    qualification = evaluate_fixed_control_qualification(
        result,
        evidence.model_copy(update={"series": saturated_series}),
    )

    assert qualification.qualified is False
    assert qualification.rejection_reasons == ("api_cpu_headroom_failed",)


def test_simulator_receipt_loss_cannot_pass_despite_ingestion_and_stable_drain():
    result = passing_result()
    count = result.reconciliation.generated
    result = result.model_copy(
        update={
            "reconciliation": result.reconciliation.model_copy(
                update={
                    "unaccounted": 357,
                    "simulator_receipts": count - 357,
                    "simulator_unique_events": count - 357,
                    "invariants_passed": False,
                }
            )
        }
    )
    assert result.ingestion_guardrails_passed
    assert result.processing_drained and result.drain_stability_confirmed
    assert not result.correctness_guardrails_passed
    qualification = evaluate_fixed_control_qualification(result, cloudwatch_evidence())
    assert not qualification.qualified
    assert "measurement_incomplete" in qualification.rejection_reasons


@mark.parametrize("count_delta", [-1, 1])
def test_request_count_mismatch_cannot_pass_with_complete_unique_receipts(
    count_delta: int,
) -> None:
    result = passing_result()
    steps = list(result.ingestion_steps)
    steps[0] = steps[0].model_copy(
        update={"observed_request_count": steps[0].expected_request_count + count_delta}
    )
    result = result.model_copy(update={"ingestion_steps": tuple(steps)})

    assert result.correctness_guardrails_passed
    assert result.drain_stability_confirmed
    assert not result.ingestion_guardrails_passed
    qualification = evaluate_fixed_control_qualification(result, cloudwatch_evidence())
    assert not qualification.qualified
    assert "measurement_incomplete" in qualification.rejection_reasons


@mark.parametrize("failure", [{"k6_exit_code": 99}, {"dropped_iteration_count": 1}])
def test_driver_failure_cannot_pass_with_exact_request_counts_and_receipts(
    failure: dict[str, int],
) -> None:
    result = passing_result().model_copy(update=failure)

    assert result.correctness_guardrails_passed
    assert all(step.ingestion_guardrails_passed for step in result.ingestion_steps)
    assert not result.ingestion_guardrails_passed
    qualification = evaluate_fixed_control_qualification(result, cloudwatch_evidence())
    assert not qualification.qualified
    assert "measurement_incomplete" in qualification.rejection_reasons


def test_rejected_candidate_saves_evidence_then_destroys(tmp_path: Path) -> None:
    aws_session = ready_session(tmp_path)
    expected = passing_result()
    cleanup: list[str] = []
    evidence = cloudwatch_evidence()
    saturated = evidence.model_copy(
        update={
            "series": tuple(
                series.model_copy(
                    update={
                        "datapoints": tuple(
                            point.model_copy(update={"value": 80})
                            for point in series.datapoints
                        )
                    }
                )
                if series.query_id == "simulator_memory"
                else series
                for series in evidence.series
            )
        }
    )

    with raises(AwsFixedControlQualificationError, match="simulator_memory"):
        run_fixed_control_session(
            aws_session,
            **approvals(aws_session),
            treatment_runner=lambda *_args, **_kwargs: expected,
            metric_collector=lambda _session, **_kwargs: saturated,
            runner=controller_runner,
            destroyer=lambda _session: cleanup.append("destroy"),
            teardown_verifier=lambda _session: cleanup.append("verify"),
        )

    assert cleanup == ["destroy", "verify"]
    assert (
        aws_session.evidence_dir / "elasticity" / "fixed" / "summary.json"
    ).is_file()


def test_fixed_control_workflow_failure_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    cleanup = []

    with raises(RuntimeError, match="simulated workload failure"):
        run_fixed_control_session(
            session,
            **approvals(session),
            treatment_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated workload failure")
            ),
            runner=controller_runner,
            destroyer=lambda _session: cleanup.append("destroy"),
            teardown_verifier=lambda _session: cleanup.append("verify"),
        )

    assert cleanup == ["destroy", "verify"]
