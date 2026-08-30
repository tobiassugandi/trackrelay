"""Tests for the small, failure-safe AWS sampler canary."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess, run
from uuid import UUID

from pytest import raises

from tests.test_aws_rehost import (
    GIT_REVISION,
    INSTANCE_ID,
    completed,
    terraform_output_name,
)
from tests.test_aws_vertical_scaling import (
    PUBLIC_IP,
    api_sample,
    prepare_rds_session,
)
from trackrelay.aws_rehost_workload import (
    RemoteRuntimeSampler,
    runtime_sampler_container_name,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    load_manifest,
    write_manifest,
)
from trackrelay.aws_vertical_scaling import (
    LoadExecutionWindow,
    TimedLoadResult,
)
from trackrelay.aws_vertical_scaling_canary import (
    VerticalScalingCanaryResult,
    VerticalScalingCanarySessionError,
    build_runtime_sampler_absence_payload,
    run_vertical_scaling_canary_point,
    run_vertical_scaling_canary_session,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    DeploymentRuntimeSample,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
)

SESSION_ID = "cloud-session-2-20260829T120000Z"


def test_canary_point_runs_only_one_short_rds_backed_load(
    tmp_path: Path,
) -> None:
    session = prepare_rds_session(tmp_path)
    actions: list[str] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        outputs = {
            "rehost_instance_id": INSTANCE_ID,
            "rehost_public_ip": PUBLIC_IP,
        }
        output_name = terraform_output_name(call)
        if output_name in outputs:
            return completed(call, stdout=outputs[output_name])
        raise AssertionError(f"unexpected external command: {call}")

    def remote_action(_session, *, action, point, **_kwargs):
        actions.append(action)
        assert point.request_rate_per_second == 10
        assert point.duration_seconds == 30
        if action == "prepare":
            return None
        return RehostServerEvidence(
            point=point,
            reconciliation=ReconciliationReport(
                test_run_id=point.test_run_id,
                generated=300,
                accepted=300,
                rejected=0,
                unique=300,
                processed=300,
                failed=0,
                pending=0,
                unaccounted=0,
                simulator_receipts=300,
                simulator_unique_events=300,
            ),
        )

    load_started_at = datetime(2026, 8, 29, 14, tzinfo=UTC)
    load_ended_at = load_started_at + timedelta(seconds=30)

    def load_executor(command, *_args, on_load_ended):
        assert "LOAD_RATE=10" in command
        assert "LOAD_DURATION_SECONDS=30" in command
        first = api_sample(load_started_at, 1)
        last = api_sample(load_ended_at, 2)
        on_load_ended()
        return TimedLoadResult(
            value=(
                0,
                (first, last),
                {
                    "metrics": {
                        "http_req_duration": {"values": {"p(95)": 10}},
                        "http_req_failed": {"values": {"rate": 0}},
                        "http_reqs": {
                            "values": {"count": 300, "rate": 10}
                        },
                        "dropped_iterations": {"values": {"count": 0}},
                    }
                },
            ),
            window=LoadExecutionWindow(
                test_run_id=UUID(int=7),
                started_at=load_started_at,
                ended_at=load_ended_at,
            ),
        )

    sampler_started_at = load_started_at - timedelta(seconds=1)

    def runtime_starter(_session, *, point, **_kwargs):
        actions.append("sampler-start")
        return RemoteRuntimeSampler(
            container_name=runtime_sampler_container_name(point),
            ready_at=sampler_started_at,
        )

    def runtime_stopper(_session, *, sampler, point, **_kwargs):
        actions.append("sampler-stop")
        assert sampler.container_name == runtime_sampler_container_name(point)

    def runtime_collector(_session, *, sampler, point, **_kwargs):
        actions.append("sampler-collect")
        assert sampler.ready_at == sampler_started_at
        first = api_sample(sampler_started_at, 1)
        last = api_sample(load_ended_at + timedelta(seconds=14), 2)
        downstream_first = first.model_copy(
            update={"process_id": 8, "database_pool": None}
        )
        downstream_last = last.model_copy(
            update={"process_id": 8, "database_pool": None}
        )
        return RehostRuntimeTimeline(
            test_run_id=point.test_run_id,
            sampling_timeout_seconds=60,
            samples=(
                DeploymentRuntimeSample(
                    api=first,
                    downstream=downstream_first,
                ),
                DeploymentRuntimeSample(
                    api=last,
                    downstream=downstream_last,
                ),
            ),
        )

    def absence_verifier(_session, *, point, **_kwargs):
        actions.append("sampler-absent")
        assert point.test_run_id == UUID(int=7)

    result = run_vertical_scaling_canary_point(
        session,
        runner=runner,
        load_executor=load_executor,
        remote_action=remote_action,
        runtime_starter=runtime_starter,
        runtime_stopper=runtime_stopper,
        runtime_collector=runtime_collector,
        absence_verifier=absence_verifier,
        now=lambda: datetime(2026, 8, 29, 15, tzinfo=UTC),
        uuid_factory=lambda: UUID(int=7),
    )

    assert result.complete_experiment_passed is True
    assert result.observed_request_count == 300
    assert actions == [
        "prepare",
        "sampler-start",
        "sampler-stop",
        "sampler-collect",
        "sampler-absent",
        "collect",
    ]
    output_root = session.evidence_dir / "vertical-scaling-canary"
    assert (output_root / "result.json").is_file()
    assert len(tuple(output_root.rglob("deployment-runtime-timeline.json"))) == 1
    assert load_manifest(session)["status"] == "vertical_scaling_canary_passed"
    portable_evidence = "\n".join(
        path.read_text(encoding="utf-8")
        for path in output_root.rglob("*.json")
    )
    assert PUBLIC_IP not in portable_evidence
    assert INSTANCE_ID not in portable_evidence


def test_absence_payload_is_narrow_and_valid_bash() -> None:
    point = RehostWorkloadPoint(
        test_run_id=UUID(int=8),
        request_rate_per_second=10,
        duration_seconds=30,
        partner_id="load-alpha",
    )
    payload = build_runtime_sampler_absence_payload(point)
    command = payload["commands"][0]

    assert runtime_sampler_container_name(point) in command
    assert "docker info" in command
    assert "docker container inspect" in command
    assert payload["executionTimeout"] == ["30"]
    syntax = run(
        ("bash", "-n"),
        input=command,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


def ready_canary_session(tmp_path: Path) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    session = AwsSession(
        session_id=SESSION_ID,
        profile="trackrelay-admin",
        region="ap-southeast-3",
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
    )
    session.evidence_dir.mkdir(parents=True)
    write_manifest(
        session,
        {
            "api_ingress_cidr": session.api_ingress_cidr,
            "approved_cost_ceiling_usd": "1.00",
            "git_revision": "a" * 40,
            "profile": session.profile,
            "region": session.region,
            "rehost_instance_type": "t3.small",
            "session_id": session.session_id,
            "status": "rds_correctness_collected",
        },
    )
    return session


def canary_result() -> VerticalScalingCanaryResult:
    started_at = datetime(2026, 8, 29, 14, tzinfo=UTC)
    return VerticalScalingCanaryResult(
        completed_at=started_at + timedelta(minutes=1),
        git_revision="a" * 40,
        test_run_id=UUID(int=9),
        load_window=LoadExecutionWindow(
            test_run_id=UUID(int=9),
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=30),
        ),
        runtime_coverage_started_at=started_at - timedelta(seconds=1),
        runtime_coverage_ended_at=started_at + timedelta(seconds=44),
    )


def approvals(session: AwsSession) -> dict[str, str]:
    return {
        "approved_session_id": session.session_id,
        "approved_cost_ceiling_usd": "1.00",
        "approved_unconditional_teardown_session_id": session.session_id,
    }


def test_canary_session_passes_then_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_canary_session(tmp_path)
    actions: list[str] = []

    observed = run_vertical_scaling_canary_session(
        session,
        **approvals(session),
        canary_runner=lambda _session: actions.append("canary")
        or canary_result(),
        destroyer=lambda _session: actions.append("destroy"),
        teardown_verifier=lambda _session: actions.append("verify"),
    )

    assert observed == canary_result()
    assert actions == ["canary", "destroy", "verify"]


def test_canary_failure_still_destroys_and_verifies(tmp_path: Path) -> None:
    session = ready_canary_session(tmp_path)
    actions: list[str] = []

    def fail(_session):
        actions.append("canary")
        raise RuntimeError("synthetic canary failure")

    with raises(RuntimeError, match="synthetic canary failure"):
        run_vertical_scaling_canary_session(
            session,
            **approvals(session),
            canary_runner=fail,
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == ["canary", "destroy", "verify"]


def test_canary_destroy_failure_does_not_skip_verification(
    tmp_path: Path,
) -> None:
    session = ready_canary_session(tmp_path)
    actions: list[str] = []

    def fail_destroy(_session):
        actions.append("destroy")
        raise RuntimeError("synthetic destroy failure")

    with raises(
        VerticalScalingCanarySessionError,
        match="destroy failure",
    ):
        run_vertical_scaling_canary_session(
            session,
            **approvals(session),
            canary_runner=lambda _session: canary_result(),
            destroyer=fail_destroy,
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == ["destroy", "verify"]


def test_canary_approval_mismatch_has_no_side_effect(tmp_path: Path) -> None:
    session = ready_canary_session(tmp_path)
    actions: list[str] = []

    with raises(AwsSessionError, match="approved canary session ID"):
        run_vertical_scaling_canary_session(
            session,
            **{
                **approvals(session),
                "approved_session_id": "cloud-session-2-20260829T120001Z",
            },
            canary_runner=lambda _session: actions.append("canary"),
            destroyer=lambda _session: actions.append("destroy"),
            teardown_verifier=lambda _session: actions.append("verify"),
        )

    assert actions == []
