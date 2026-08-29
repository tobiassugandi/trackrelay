"""Run one fast AWS sampler canary and always verify teardown."""

import json
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from shlex import quote
from signal import SIGTERM, getsignal, signal
from typing import Literal, NoReturn
from uuid import UUID, uuid4

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from trackrelay.aws_rehost import (
    INSTANCE_ID_PATTERN,
    AwsRehostError,
    ProcessRunner,
    require_applied_clean_revision,
    run_process,
    run_ssm_payload,
    terraform_output,
)
from trackrelay.aws_rehost_workload import (
    RemoteRuntimeSampler,
    cleanup_remote_runtime_sampling,
    collect_remote_runtime_sampling,
    run_remote_action,
    runtime_sampler_container_name,
    start_remote_runtime_sampling,
    stop_remote_runtime_sampling,
    validate_runtime_timeline_covers_load,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    destroy_session,
    load_manifest,
    parse_positive_money,
    session_from_arguments,
    verify_destroyed,
    write_manifest,
)
from trackrelay.aws_vertical_scaling import (
    LoadExecutionWindow,
    TimedLocalLoadExecutor,
    build_experiment_definition,
    execute_timed_local_load,
)
from trackrelay.experiments.generator import write_input_manifest
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    RuntimeMetricsSamples,
    build_k6_command,
    derive_performance_result,
)
from trackrelay.experiments.rehost import (
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
)
from trackrelay.experiments.vertical_scaling import (
    ECONOMICAL_BASELINE_INSTANCE_TYPE,
)

CANARY_RATE_PER_SECOND = 10
CANARY_DURATION_SECONDS = 30
CanaryAction = Callable[..., object]


class VerticalScalingCanaryResult(BaseModel):
    """Small portable proof that the repaired sampler works in AWS."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-detached-runtime-sampler-canary-v1"] = (
        "aws-detached-runtime-sampler-canary-v1"
    )
    completed_at: AwareDatetime
    git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    instance_type: Literal["t4g.small"] = "t4g.small"
    database_placement: Literal["private-single-az-rds"] = (
        "private-single-az-rds"
    )
    test_run_id: UUID
    request_rate_per_second: Literal[10] = CANARY_RATE_PER_SECOND
    duration_seconds: Literal[30] = CANARY_DURATION_SECONDS
    expected_request_count: Literal[300] = 300
    observed_request_count: Literal[300] = 300
    load_window: LoadExecutionWindow
    runtime_coverage_started_at: AwareDatetime
    runtime_coverage_ended_at: AwareDatetime
    reconciliation_invariants_passed: Literal[True] = True
    complete_experiment_passed: Literal[True] = True
    sampler_container_absent: Literal[True] = True

    @model_validator(mode="after")
    def require_matching_contained_timeline(
        self,
    ) -> "VerticalScalingCanaryResult":
        if self.load_window.test_run_id != self.test_run_id:
            raise ValueError("canary load-window identity differs")
        if self.runtime_coverage_started_at > self.load_window.started_at:
            raise ValueError("canary runtime sampling began after load")
        if self.runtime_coverage_ended_at < self.load_window.ended_at:
            raise ValueError("canary runtime sampling ended before load")
        return self


class VerticalScalingCanarySessionError(RuntimeError):
    """Report canary cleanup failures without hiding its primary failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException | None = None,
        cleanup_errors: Sequence[BaseException] = (),
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


def _write_model(
    model: BaseModel,
    path: Path,
    *,
    exclude_computed_fields: bool = False,
) -> None:
    path.write_text(
        model.model_dump_json(
            indent=2,
            exclude_computed_fields=exclude_computed_fields,
        )
        + "\n",
        encoding="utf-8",
    )


def build_runtime_sampler_absence_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Prove Docker is reachable and the run-specific sampler is absent."""
    container_name = quote(runtime_sampler_container_name(point))
    command = "\n".join(
        (
            "set -euo pipefail",
            "docker info >/dev/null",
            f"if docker container inspect {container_name} >/dev/null 2>&1; then",
            "  exit 1",
            "fi",
        )
    )
    return {"commands": [command], "executionTimeout": ["30"]}


def verify_remote_runtime_sampler_absent(
    session: AwsSession,
    *,
    instance_id: str,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> None:
    """Require successful removal of the detached sampler container."""
    run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_runtime_sampler_absence_payload(point),
        comment="TrackRelay detached sampler absence canary",
        runner=runner,
    )


def run_vertical_scaling_canary_point(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    load_executor: TimedLocalLoadExecutor = execute_timed_local_load,
    remote_action: Callable[..., RehostServerEvidence | None] = (
        run_remote_action
    ),
    runtime_starter: Callable[..., RemoteRuntimeSampler] = (
        start_remote_runtime_sampling
    ),
    runtime_collector: Callable[..., RehostRuntimeTimeline] = (
        collect_remote_runtime_sampling
    ),
    runtime_stopper: Callable[..., None] = stop_remote_runtime_sampling,
    runtime_cleaner: Callable[..., None] = cleanup_remote_runtime_sampling,
    absence_verifier: Callable[..., None] = (
        verify_remote_runtime_sampler_absent
    ),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uuid_factory: Callable[[], UUID] = uuid4,
) -> VerticalScalingCanaryResult:
    """Run one non-publishable 10 events/s point against the RDS rehost."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "rds_correctness_collected":
        raise AwsRehostError("the canary requires completed RDS correctness")
    if session.rehost_instance_type != ECONOMICAL_BASELINE_INSTANCE_TYPE:
        raise AwsRehostError("the canary must use t4g.small")
    definition = build_experiment_definition(
        session,
        manifest=manifest,
        revision=revision,
        prepared_at=now(),
    )
    controls = definition.controls.workload
    instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")
    public_ip_text = terraform_output(
        session,
        "rehost_public_ip",
        runner=runner,
    )
    try:
        public_ip = IPv4Address(public_ip_text)
    except ValueError as error:
        raise AwsRehostError(
            "Terraform returned an invalid public IPv4"
        ) from error

    output_root = session.evidence_dir / "vertical-scaling-canary"
    output_root.mkdir(parents=True, exist_ok=False)
    test_run_id = uuid_factory()
    run_directory = output_root / str(test_run_id)
    run_directory.mkdir()
    input_manifest_path = run_directory / "input-manifest.json"
    k6_summary_path = run_directory / "k6-summary.json"
    local_runtime_path = run_directory / "runtime-metrics-samples.json"
    deployment_runtime_path = (
        run_directory / "deployment-runtime-timeline.json"
    )
    server_evidence_path = run_directory / "server-evidence.json"
    performance_result_path = run_directory / "performance-result.json"

    point = RehostWorkloadPoint(
        test_run_id=test_run_id,
        request_rate_per_second=CANARY_RATE_PER_SECOND,
        duration_seconds=CANARY_DURATION_SECONDS,
        random_seed=controls.random_seed,
        partner_id=controls.partner_id,
        post_load_settle_timeout_seconds=(
            controls.post_load_settle_timeout_seconds
        ),
        post_load_stable_window_seconds=(
            controls.post_load_stable_window_seconds
        ),
    )
    write_input_manifest(point.manifest(), input_manifest_path)
    remote_action(
        session,
        instance_id=instance_id,
        action="prepare",
        point=point,
        runner=runner,
    )

    api_url = f"http://{public_ip}:8000"
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
        request_rate_per_second=CANARY_RATE_PER_SECOND,
        duration_seconds=CANARY_DURATION_SECONDS,
        random_seed=controls.random_seed,
        partner_id=controls.partner_id,
        resource_sample_interval_seconds=(
            controls.runtime_sample_interval_seconds
        ),
        post_load_settle_timeout_seconds=(
            controls.post_load_settle_timeout_seconds
        ),
        post_load_stable_window_seconds=(
            controls.post_load_stable_window_seconds
        ),
        k6_image=controls.k6_image,
        trackrelay_api_url=api_url,
        trackrelay_api_url_for_container=api_url,
    )
    command = build_k6_command(
        configuration,
        manifest_path=input_manifest_path,
        run_directory=run_directory,
    )
    runtime_sampler = runtime_starter(
        session,
        instance_id=instance_id,
        point=point,
        runner=runner,
    )

    def stop_sampler_after_load() -> None:
        runtime_stopper(
            session,
            instance_id=instance_id,
            sampler=runtime_sampler,
            point=point,
            runner=runner,
        )

    try:
        with httpx.Client(base_url=api_url, timeout=30.0) as client:
            timed_load = load_executor(
                command,
                client,
                controls.runtime_sample_interval_seconds,
                k6_summary_path,
                test_run_id,
                now,
                on_load_ended=stop_sampler_after_load,
            )
    except BaseException as load_error:
        try:
            runtime_cleaner(
                session,
                instance_id=instance_id,
                point=point,
                runner=runner,
            )
        except (AwsRehostError, OSError) as cleanup_error:
            load_error.add_note(
                "detached runtime sampler cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        raise

    k6_exit_code, local_samples, k6_summary = timed_load.value
    runtime_timeline = runtime_collector(
        session,
        instance_id=instance_id,
        sampler=runtime_sampler,
        point=point,
        runner=runner,
    )
    validate_runtime_timeline_covers_load(
        runtime_timeline,
        test_run_id=test_run_id,
        load_started_at=timed_load.window.started_at,
        load_ended_at=timed_load.window.ended_at,
    )
    absence_verifier(
        session,
        instance_id=instance_id,
        point=point,
        runner=runner,
    )
    server_evidence = remote_action(
        session,
        instance_id=instance_id,
        action="collect",
        point=point,
        runner=runner,
    )
    if server_evidence is None:
        raise AwsRehostError("SSM canary collection returned no evidence")
    performance_result = derive_performance_result(
        configuration,
        test_run_id=test_run_id,
        k6_exit_code=k6_exit_code,
        k6_summary=k6_summary,
        resource_samples=local_samples,
        reconciliation=server_evidence.reconciliation,
    )

    _write_model(timed_load.window, run_directory / "load-window.json")
    _write_model(
        RuntimeMetricsSamples(
            test_run_id=test_run_id,
            samples=local_samples,
        ),
        local_runtime_path,
    )
    _write_model(runtime_timeline, deployment_runtime_path)
    _write_model(server_evidence, server_evidence_path)
    if not k6_summary_path.exists():
        k6_summary_path.write_text(
            f"{json.dumps(k6_summary, indent=2)}\n",
            encoding="utf-8",
        )
    portable_configuration = configuration.model_copy(
        update={
            "trackrelay_api_url": "http://canary-api.invalid",
            "trackrelay_api_url_for_container": "http://canary-api.invalid",
            "downstream_url": "http://private-downstream.invalid",
        }
    )
    _write_model(
        performance_result.model_copy(
            update={"configuration": portable_configuration}
        ),
        performance_result_path,
        exclude_computed_fields=True,
    )
    if not performance_result.complete_experiment_passed:
        raise AwsRehostError(
            "the AWS sampler canary failed execution, SLO, or reconciliation"
        )

    result = VerticalScalingCanaryResult(
        completed_at=now(),
        git_revision=revision,
        test_run_id=test_run_id,
        observed_request_count=performance_result.observed_request_count,
        load_window=timed_load.window,
        runtime_coverage_started_at=runtime_timeline.coverage_started_at,
        runtime_coverage_ended_at=runtime_timeline.coverage_ended_at,
    )
    result_path = output_root / "result.json"
    _write_model(result, result_path)
    manifest.update(
        {
            "status": "vertical_scaling_canary_passed",
            "vertical_scaling_canary": {
                "completed_at": result.completed_at.isoformat(),
                "result": "vertical-scaling-canary/result.json",
            },
        }
    )
    write_manifest(session, manifest)
    return result


def validate_canary_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> None:
    """Arm only the approved one-point canary and mandatory teardown."""
    if not session.session_id.startswith("cloud-session-2-"):
        raise AwsSessionError("the Stage 9.3 canary must use cloud session 2")
    if session.rehost_instance_type != ECONOMICAL_BASELINE_INSTANCE_TYPE:
        raise AwsSessionError("the canary must use t4g.small")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved canary session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "rds_correctness_collected":
        raise AwsSessionError("the canary requires completed RDS correctness")
    recorded_ceiling = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded_ceiling, str):
        raise AwsSessionError("the applied canary has no approved cost ceiling")
    approved_ceiling = parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved canary cost ceiling",
    )
    if approved_ceiling != parse_positive_money(
        recorded_ceiling,
        field_name="recorded canary cost ceiling",
    ):
        raise AwsSessionError("approved canary cost ceiling differs from apply")


def run_vertical_scaling_canary_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    canary_runner: CanaryAction = run_vertical_scaling_canary_point,
    destroyer: CanaryAction = destroy_session,
    teardown_verifier: CanaryAction = verify_destroyed,
) -> VerticalScalingCanaryResult:
    """Run one canary; after approval, cleanup is never conditional."""
    validate_canary_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    result: VerticalScalingCanaryResult | None = None
    workflow_error: BaseException | None = None
    try:
        observed = canary_runner(session)
        if not isinstance(observed, VerticalScalingCanaryResult):
            raise TypeError("the canary runner returned invalid evidence")
        result = observed
    except BaseException as error:  # noqa: BLE001 - teardown follows interrupts
        workflow_error = error

    cleanup_errors: list[BaseException] = []
    try:
        destroyer(session)
    except BaseException as error:  # noqa: BLE001 - verification must still run
        cleanup_errors.append(error)
    try:
        teardown_verifier(session)
    except BaseException as error:  # noqa: BLE001 - report every cleanup failure
        cleanup_errors.append(error)

    if cleanup_errors:
        message = "canary cleanup did not complete: " + "; ".join(
            f"{type(error).__name__}: {error}" for error in cleanup_errors
        )
        if workflow_error is not None:
            message = (
                f"canary failed with {type(workflow_error).__name__}: "
                f"{workflow_error}; {message}"
            )
        raise VerticalScalingCanarySessionError(
            message,
            workflow_error=workflow_error,
            cleanup_errors=cleanup_errors,
        ) from (workflow_error or cleanup_errors[0])
    if workflow_error is not None:
        raise workflow_error
    if result is None:
        raise AssertionError("the canary completed without a result")
    return result


def build_parser() -> ArgumentParser:
    """Build the explicitly armed one-point canary command."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument(
        "--approved-unconditional-teardown-session-id",
        required=True,
    )
    return parser


def run_from_arguments(arguments: Namespace) -> VerticalScalingCanaryResult:
    """Build the shared AWS session and execute the armed canary."""
    return run_vertical_scaling_canary_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    """Translate SIGTERM so the canary's teardown path remains active."""
    raise KeyboardInterrupt("received SIGTERM during the AWS canary")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the canary and translate ordinary failures concisely."""
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        result = run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsRehostError,
        AwsSessionError,
        KeyboardInterrupt,
        VerticalScalingCanarySessionError,
    ) as error:
        raise SystemExit(f"AWS sampler canary failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    print("AWS sampler canary passed and teardown was verified")
    print(f"canary run ID: {result.test_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
