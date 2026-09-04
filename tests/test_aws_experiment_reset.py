"""Tests for the guarded Stage 9.6 between-treatment reset."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from uuid import UUID

import httpx
from pytest import MonkeyPatch, mark, raises

from trackrelay.aws_diagnostics import error_evidence
from trackrelay.aws_experiment_reset import (
    AwsExperimentResetError,
    ExperimentResetObservation,
    ExperimentResetResult,
    ResetQueueState,
    execute_experiment_reset,
    run_experiment_reset_session,
    validate_experiment_reset_approval,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)
from trackrelay.downstream.control import SimulatorMode
from trackrelay.operator_status import progress_output
from trackrelay.services.experiment_reset import (
    ExperimentDatabaseCounts,
    ExperimentResetEvidence,
    ExperimentStateSnapshot,
)

SESSION_ID = "cloud-session-4-20260903T090000Z"
GIT_REVISION = "e" * 40
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000948")
API_URL = "http://trackrelay-reset.example.com"
SOURCE_QUEUE_URL = (
    "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-delivery"
)
DLQ_URL = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/trackrelay-dlq"
CLUSTER_NAME = "trackrelay-8a7e37db-async"
WORKER_SERVICE = "trackrelay-8a7e37db-worker"
STARTED_AT = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
EXPECTED_COUNT = 2


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, 0, stdout, "")


def ready_session(
    tmp_path: Path, *, status: str = "fixed_control_qualified"
) -> AwsSession:
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
    fixed_dir = session.evidence_dir / "elasticity" / "fixed"
    fixed_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "5.00",
            "deployment_mode": "async",
            "fixed_control": {"summary": "elasticity/fixed/summary.json"},
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


def fixed_summary_stub() -> SimpleNamespace:
    return SimpleNamespace(
        measurement=SimpleNamespace(
            test_run_id=TEST_RUN_ID,
            definition=SimpleNamespace(expected_request_count=EXPECTED_COUNT),
        )
    )


def database_counts(count: int) -> ExperimentDatabaseCounts:
    return ExperimentDatabaseCounts(
        delivery_attempts=count,
        delivery_outbox_entries=count,
        events=count,
        shipments=count,
        test_runs=1 if count else 0,
    )


def application_state(count: int) -> ExperimentStateSnapshot:
    return ExperimentStateSnapshot(
        database=database_counts(count),
        simulator_receipts=count,
        simulator_mode=SimulatorMode.HEALTHY,
    )


def application_reset() -> ExperimentResetEvidence:
    return ExperimentResetEvidence(
        test_run_id=TEST_RUN_ID,
        expected_event_count=EXPECTED_COUNT,
        before=application_state(EXPECTED_COUNT),
        after=application_state(0),
        simulator_receipts_removed=EXPECTED_COUNT,
    )


def observation(observed_at: datetime, seconds_after_purge: float):
    empty_queue = ResetQueueState(
        visible_messages=0,
        in_flight_messages=0,
        delayed_messages=0,
    )
    return ExperimentResetObservation(
        observed_at=observed_at,
        seconds_after_purge=seconds_after_purge,
        application=application_state(0),
        source_queue=empty_queue,
        dead_letter_queue=empty_queue,
        worker_desired_count=1,
        worker_running_count=1,
        worker_pending_count=0,
    )


def reset_result() -> ExperimentResetResult:
    observations = tuple(
        observation(STARTED_AT + timedelta(seconds=seconds), seconds)
        for seconds in (60, 70, 80, 90)
    )
    return ExperimentResetResult(
        fixed_test_run_id=TEST_RUN_ID,
        started_at=STARTED_AT,
        completed_at=STARTED_AT + timedelta(seconds=90),
        application_reset=application_reset(),
        observations=observations,
        autoscaling_target_absent_before=True,
        autoscaling_target_absent_after=True,
    )


def terraform_output_name(arguments: Sequence[str]) -> str | None:
    call = tuple(arguments)
    if len(call) >= 5 and call[2] == "output":
        return call[4]
    return None


def preparation_runner(arguments: Sequence[str], _input: str | None):
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
                "scaling_metrics_namespace": "TrackRelay/Elasticity",
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


def test_reset_requires_a_qualified_fixed_control(tmp_path: Path) -> None:
    session = ready_session(tmp_path, status="fixed_control_armed")

    with raises(AwsSessionError, match="qualified fixed control"):
        validate_experiment_reset_approval(session, **approvals(session))


def test_execution_resets_then_proves_empty_for_thirty_seconds(
    tmp_path: Path,
) -> None:
    session = ready_session(tmp_path)
    evidence_root = tmp_path / "reset"
    evidence_root.mkdir()
    current_time = STARTED_AT
    reset_applied = False
    purged: list[str] = []

    def clock() -> datetime:
        return current_time

    def sleeper(seconds: float) -> None:
        nonlocal current_time
        current_time += timedelta(seconds=seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reset_applied
        if request.method == "GET" and request.url.path.endswith("/state"):
            state = application_state(0 if reset_applied else EXPECTED_COUNT)
            return httpx.Response(
                200, json=state.model_dump(mode="json"), request=request
            )
        if request.method == "POST" and request.url.path.endswith("/reset"):
            assert loads(request.content) == {
                "test_run_id": str(TEST_RUN_ID),
                "expected_event_count": EXPECTED_COUNT,
            }
            reset_applied = True
            return httpx.Response(
                200,
                json=application_reset().model_dump(mode="json"),
                request=request,
            )
        raise AssertionError((request.method, request.url.path))

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        if "get-queue-attributes" in call:
            names = call[call.index("--attribute-names") + 1 : call.index("--query")]
            return completed(call, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in call:
            return completed(call, stdout="[1, 1, 0]")
        if "describe-scalable-targets" in call:
            return completed(call, stdout="[]")
        if "purge-queue" in call:
            purged.append(call[call.index("--queue-url") + 1])
            return completed(call)
        raise AssertionError(call)

    with httpx.Client(
        base_url=API_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = execute_experiment_reset(
            session,
            fixed_summary=fixed_summary_stub(),
            api_url=API_URL,
            source_queue_url=SOURCE_QUEUE_URL,
            dead_letter_queue_url=DLQ_URL,
            cluster_name=CLUSTER_NAME,
            worker_service_name=WORKER_SERVICE,
            evidence_root=evidence_root,
            runner=runner,
            now=clock,
            sleeper=sleeper,
            client=client,
        )

    assert purged == [SOURCE_QUEUE_URL, DLQ_URL]
    assert result.observations[-1].seconds_after_purge == 90
    assert (evidence_root / "pre-reset-observation.json").is_file()
    assert (evidence_root / "application-reset.json").is_file()
    assert (evidence_root / "result.json").is_file()


def test_execution_refuses_worker_autoscaling_before_reset(tmp_path: Path) -> None:
    session = ready_session(tmp_path)
    evidence_root = tmp_path / "reset"
    evidence_root.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=application_state(EXPECTED_COUNT).model_dump(mode="json"),
            request=request,
        )

    def runner(arguments: Sequence[str], _input: str | None):
        call = tuple(arguments)
        if "get-queue-attributes" in call:
            names = call[call.index("--attribute-names") + 1 : call.index("--query")]
            return completed(call, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in call:
            return completed(call, stdout="[1, 1, 0]")
        if "describe-scalable-targets" in call:
            return completed(call, stdout='[{"MinCapacity": 1}]')
        raise AssertionError(call)

    with (
        httpx.Client(
            base_url=API_URL, transport=httpx.MockTransport(handler)
        ) as client,
        raises(AwsExperimentResetError, match="autoscaling appeared"),
    ):
        execute_experiment_reset(
            session,
            fixed_summary=fixed_summary_stub(),
            api_url=API_URL,
            source_queue_url=SOURCE_QUEUE_URL,
            dead_letter_queue_url=DLQ_URL,
            cluster_name=CLUSTER_NAME,
            worker_service_name=WORKER_SERVICE,
            evidence_root=evidence_root,
            runner=runner,
            client=client,
        )


def test_successful_reset_leaves_stack_for_elastic_treatment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = ready_session(tmp_path)
    cleanup: list[str] = []
    expected = reset_result()
    monkeypatch.setattr(
        "trackrelay.aws_experiment_reset.validate_experiment_reset_approval",
        lambda *_args, **_kwargs: (load_manifest(session), fixed_summary_stub()),
    )

    result = run_experiment_reset_session(
        session,
        **approvals(session),
        reset_runner=lambda *_args, **_kwargs: expected,
        runner=preparation_runner,
        destroyer=lambda _session: cleanup.append("destroy"),
        teardown_verifier=lambda _session: cleanup.append("verify"),
    )

    assert result == expected
    assert cleanup == []
    manifest = load_manifest(session)
    assert manifest["status"] == "experiment_reset_verified"
    assert manifest["experiment_reset"]["result"] == "elasticity/reset/result.json"


def test_reset_workflow_failure_destroys_and_verifies(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = ready_session(tmp_path)
    cleanup: list[str] = []
    monkeypatch.setattr(
        "trackrelay.aws_experiment_reset.validate_experiment_reset_approval",
        lambda *_args, **_kwargs: (load_manifest(session), fixed_summary_stub()),
    )

    with raises(RuntimeError, match="simulated reset failure"):
        run_experiment_reset_session(
            session,
            **approvals(session),
            reset_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated reset failure")
            ),
            runner=preparation_runner,
            destroyer=lambda _session: cleanup.append("destroy"),
            teardown_verifier=lambda _session: cleanup.append("verify"),
        )

    assert cleanup == ["destroy", "verify"]


@mark.parametrize(
    ("failed_method", "failure"),
    [
        ("GET", 503),
        ("POST", 409),
        ("POST", 422),
        ("POST", 503),
        ("GET", "timeout"),
        ("POST", "timeout"),
    ],
)
def test_http_failure_reports_safe_operation_before_cleanup_without_retry(
    tmp_path,
    monkeypatch,
    failed_method,
    failure,
):
    session = ready_session(tmp_path)
    monkeypatch.setattr(
        "trackrelay.aws_experiment_reset.validate_experiment_reset_approval",
        lambda *_args, **_kwargs: (load_manifest(session), fixed_summary_stub()),
    )
    calls = []
    messages = []
    private = "private-password-token-and-payload"

    def handler(request):
        calls.append(request.method)
        if request.method == failed_method:
            if failure == "timeout":
                raise httpx.ReadTimeout(private, request=request)
            return httpx.Response(
                failure,
                json={"detail": private},
                headers={"X-Private": private},
                request=request,
            )
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=application_state(EXPECTED_COUNT).model_dump(mode="json"),
            request=request,
        )

    def runner(arguments, input_text):
        if "get-queue-attributes" in arguments:
            names = arguments[
                arguments.index("--attribute-names") + 1 : arguments.index("--query")
            ]
            return completed(arguments, stdout=dumps({name: "0" for name in names}))
        if "describe-services" in arguments:
            return completed(arguments, stdout="[1, 1, 0]")
        if "describe-scalable-targets" in arguments:
            return completed(arguments, stdout="[]")
        assert "purge-queue" not in arguments
        return preparation_runner(arguments, input_text)

    with (
        httpx.Client(
            base_url=f"http://user:{private}@private-endpoint.invalid",
            transport=httpx.MockTransport(handler),
        ) as client,
        progress_output(messages.append, repeat_interval_seconds=0),
        raises(AwsExperimentResetError) as caught,
    ):
        run_experiment_reset_session(
            session,
            **approvals(session),
            runner=runner,
            reset_runner=lambda *args, **kwargs: execute_experiment_reset(
                *args,
                **kwargs,
                client=client,
            ),
            destroyer=lambda _session: messages.append("destroy"),
            teardown_verifier=lambda _session: messages.append("verify"),
        )

    assert calls == (["GET"] if failed_method == "GET" else ["GET", "POST"])
    path = (
        "/api/v1/experiments/state"
        if failed_method == "GET"
        else "/api/v1/experiments/reset"
    )
    reason = "ReadTimeout" if failure == "timeout" else f"HTTP {failure}"
    expected = f"Experiment reset {failed_method} {path} failed: {reason}"
    assert str(caught.value) == expected
    assert expected in messages[0]
    assert messages[-2:] == ["destroy", "verify"]
    evidence = error_evidence(caught.value)
    assert evidence["message"] == expected
    assert evidence["cause"]["type"] == (
        "ReadTimeout" if failure == "timeout" else "HTTPStatusError"
    )
    safe_output = dumps(evidence) + " ".join(messages)
    assert private not in safe_output
    assert "private-endpoint" not in safe_output
    assert "X-Private" not in safe_output
