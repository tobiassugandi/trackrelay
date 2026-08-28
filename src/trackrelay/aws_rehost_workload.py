"""Run the frozen workload against a deployed synchronous AWS rehost."""

import json
import os
import platform
from argparse import Namespace
from base64 import b64decode
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from gzip import decompress
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from trackrelay.aws_rehost import (
    COMMAND_ID_PATTERN,
    INSTANCE_ID_PATTERN,
    AwsRehostError,
    ProcessRunner,
    aws_prefix,
    invoke,
    require_applied_clean_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_session import (
    AwsSession,
    file_sha256,
    session_from_arguments,
    write_manifest,
)
from trackrelay.experiments.baseline import (
    DEFAULT_RATES,
    LegacyBaselineBenchmarkDefinition,
    LegacyBaselineRateResult,
    compact_rate_result,
    derive_legacy_baseline_summary,
)
from trackrelay.experiments.generator import write_input_manifest
from trackrelay.experiments.performance import (
    PerformanceExperimentConfiguration,
    RuntimeMetricsSamples,
    build_k6_command,
    derive_performance_result,
    run_k6_with_resource_sampling,
)
from trackrelay.experiments.rehost import (
    EVIDENCE_PREFIX,
    RUNTIME_EVIDENCE_PREFIX,
    RUNTIME_SAMPLING_MARGIN_SECONDS,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
)
from trackrelay.experiments.slo import (
    INITIAL_BASELINE_SLO,
    BaselineSLODefinition,
)
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
FROZEN_BASELINE_DEFINITION = (
    Path(__file__).resolve().parents[2]
    / "results"
    / "legacy-baseline"
    / "benchmark-definition.json"
)
LocalLoadExecutor = Callable[
    [Sequence[str], httpx.Client, float, Path],
    tuple[int, tuple[RuntimeMetricsSnapshot, ...], dict[str, object]],
]
Sleeper = Callable[[float], None]


class BenchmarkDriverEnvironment(BaseModel):
    """Non-secret local context for interpreting the workload execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operating_system: str
    architecture: str
    logical_cpu_count: PositiveInteger
    python_version: str


class RehostWorkloadDefinition(BaseModel):
    """Portable Stage 9.1 workload and deployment facts, without endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-synchronous-rehost-v1"] = (
        "aws-synchronous-rehost-v1"
    )
    architecture: Literal["aws-synchronous-rehost"] = (
        "aws-synchronous-rehost"
    )
    source_workload: Literal["legacy-local-baseline-v1"] = (
        "legacy-local-baseline-v1"
    )
    source_benchmark_definition_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    aws_region: str
    ec2_instance_type: Literal["t4g.small"] = "t4g.small"
    api_process_count: Literal[1] = 1
    downstream_process_count: Literal[1] = 1
    database_placement: Literal["same-ec2-host-compose-container"] = (
        "same-ec2-host-compose-container"
    )
    benchmark_driver_placement: Literal["local-developer-machine"] = (
        "local-developer-machine"
    )
    offered_rates_per_second: tuple[PositiveInteger, ...] = DEFAULT_RATES
    tier_duration_seconds: PositiveInteger = 10
    random_seed: int = 20260806
    partner_id: str = "load-alpha"
    start_at: AwareDatetime
    resource_sample_interval_seconds: Annotated[float, Field(gt=0)] = 0.25
    post_load_settle_timeout_seconds: Annotated[float, Field(gt=0)] = 30
    post_load_stable_window_seconds: Annotated[float, Field(gt=0)] = 2
    k6_image: str = "grafana/k6:2.1.0"
    slo: BaselineSLODefinition = INITIAL_BASELINE_SLO
    benchmark_driver: BenchmarkDriverEnvironment


class RehostWorkloadSummary(BaseModel):
    """Compact Stage 9.1 portability result across all frozen rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    benchmark_name: Literal["aws-synchronous-rehost-v1"] = (
        "aws-synchronous-rehost-v1"
    )
    completed_at: AwareDatetime
    maximum_sustainable_rate_per_second: PositiveInteger | None
    first_failing_rate_per_second: PositiveInteger | None
    capacity_is_at_least_highest_tested_rate: bool
    rate_results: tuple[LegacyBaselineRateResult, ...]


def capture_benchmark_driver_environment() -> BenchmarkDriverEnvironment:
    """Capture context without saving hostnames, addresses, or credentials."""
    return BenchmarkDriverEnvironment(
        operating_system=platform.platform(),
        architecture=platform.machine() or "unknown",
        logical_cpu_count=os.cpu_count() or 1,
        python_version=platform.python_version(),
    )


def frozen_rehost_definition(
    *,
    revision: str,
    region: str,
    source_path: Path = FROZEN_BASELINE_DEFINITION,
) -> RehostWorkloadDefinition:
    """Copy the committed Step 8.6 workload without its local endpoints."""
    try:
        source = LegacyBaselineBenchmarkDefinition.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
    except OSError as error:
        raise AwsRehostError(
            "the frozen Step 8.6 benchmark definition is unavailable"
        ) from error
    legacy = source.configuration
    return RehostWorkloadDefinition(
        git_revision=revision,
        aws_region=region,
        source_benchmark_definition_sha256=file_sha256(source_path),
        offered_rates_per_second=legacy.offered_rates_per_second,
        tier_duration_seconds=legacy.tier_duration_seconds,
        random_seed=legacy.random_seed,
        partner_id=legacy.partner_id,
        start_at=legacy.start_at,
        resource_sample_interval_seconds=(
            legacy.resource_sample_interval_seconds
        ),
        post_load_settle_timeout_seconds=(
            legacy.post_load_settle_timeout_seconds
        ),
        post_load_stable_window_seconds=(
            legacy.post_load_stable_window_seconds
        ),
        k6_image=legacy.k6_image,
        slo=legacy.slo,
        benchmark_driver=capture_benchmark_driver_environment(),
    )


def _write_model(model: BaseModel, path: Path) -> None:
    path.write_text(f"{model.model_dump_json(indent=2)}\n", encoding="utf-8")


def build_remote_action_payload(
    action: Literal["prepare", "collect"],
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Build a secret-free private-network experiment command."""
    command = " ".join(
        (
            "docker compose",
            "--project-name trackrelay-rehost",
            "--env-file /opt/trackrelay/.env",
            "--file /opt/trackrelay/compose.yaml",
            "run --rm --no-deps api",
            "python -m trackrelay.experiments.rehost",
            action,
            f"--test-run-id {point.test_run_id}",
            f"--rate {point.request_rate_per_second}",
            f"--duration-seconds {point.duration_seconds}",
            f"--seed {point.random_seed}",
            f"--partner-id {point.partner_id}",
            f"--start-at {point.start_at.isoformat()}",
            (
                "--settle-timeout-seconds "
                f"{point.post_load_settle_timeout_seconds}"
            ),
            (
                "--stable-window-seconds "
                f"{point.post_load_stable_window_seconds}"
            ),
        )
    )
    payload = {
        "commands": [f"set -euo pipefail\n{command}"],
        "executionTimeout": ["120"],
    }
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM workload payload exceeds the safety limit")
    return payload


def build_runtime_sampling_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Build the private five-second API/downstream sampling command."""
    sampling_duration = (
        point.duration_seconds + RUNTIME_SAMPLING_MARGIN_SECONDS
    )
    command = " ".join(
        (
            "docker compose",
            "--project-name trackrelay-rehost",
            "--env-file /opt/trackrelay/.env",
            "--file /opt/trackrelay/compose.yaml",
            "run --rm --no-deps api",
            "python -m trackrelay.experiments.rehost",
            "sample-runtime",
            f"--test-run-id {point.test_run_id}",
            f"--rate {point.request_rate_per_second}",
            f"--duration-seconds {point.duration_seconds}",
            f"--sampling-duration-seconds {sampling_duration}",
            f"--seed {point.random_seed}",
            f"--partner-id {point.partner_id}",
            f"--start-at {point.start_at.isoformat()}",
            (
                "--settle-timeout-seconds "
                f"{point.post_load_settle_timeout_seconds}"
            ),
            (
                "--stable-window-seconds "
                f"{point.post_load_stable_window_seconds}"
            ),
        )
    )
    payload = {
        "commands": [f"set -euo pipefail\n{command}"],
        "executionTimeout": [str(sampling_duration + 120)],
    }
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM runtime-sampling payload exceeds the safety limit")
    return payload


def start_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
    sleeper: Sleeper = sleep,
) -> str:
    """Start private sampling and wait until its first process reads succeed."""
    payload = build_runtime_sampling_payload(point)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="trackrelay-runtime-ssm-",
        suffix=".json",
    ) as payload_file:
        payload_file.write(json.dumps(payload))
        payload_file.flush()
        command_id = invoke(
            runner,
            (
                *aws_prefix(session),
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--comment",
                "TrackRelay private runtime sampling",
                "--parameters",
                f"file://{payload_file.name}",
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ),
            action="SSM runtime sampling start",
        ).stdout.strip()
    if COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise AwsRehostError("SSM returned an invalid runtime command ID")

    output_command = (
        *aws_prefix(session),
        "ssm",
        "get-command-invocation",
        "--command-id",
        command_id,
        "--instance-id",
        instance_id,
        "--query",
        "StandardOutputContent",
        "--output",
        "text",
    )
    for _ in range(40):
        result = runner(output_command, None)
        if (
            result.returncode == 0
            and "TRACKRELAY_RUNTIME_SAMPLING_READY" in result.stdout
        ):
            return command_id
        sleeper(0.5)
    raise AwsRehostError("private runtime sampling did not become ready")


def collect_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    command_id: str,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> RehostRuntimeTimeline:
    """Wait for and decode one compressed private process timeline."""
    invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "wait",
            "command-executed",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
        ),
        action="SSM runtime sampling completion",
    )
    status = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "[Status,ResponseCode]",
            "--output",
            "text",
        ),
        action="SSM runtime sampling status",
    ).stdout.split()
    if status != ["Success", "0"]:
        raise AwsRehostError("SSM runtime sampling did not report success")
    output = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "StandardOutputContent",
            "--output",
            "text",
        ),
        action="SSM runtime evidence collection",
    ).stdout
    evidence_lines = [
        line.removeprefix(RUNTIME_EVIDENCE_PREFIX)
        for line in output.splitlines()
        if line.startswith(RUNTIME_EVIDENCE_PREFIX)
    ]
    if len(evidence_lines) != 1:
        raise AwsRehostError("SSM returned ambiguous runtime evidence")
    try:
        evidence_json = decompress(
            b64decode(evidence_lines[0], validate=True)
        ).decode("utf-8")
        timeline = RehostRuntimeTimeline.model_validate_json(evidence_json)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise AwsRehostError("SSM returned invalid runtime evidence") from error
    if timeline.test_run_id != point.test_run_id:
        raise AwsRehostError("runtime evidence identity differs from the workload")
    return timeline


def run_remote_action(
    session: AwsSession,
    *,
    instance_id: str,
    action: Literal["prepare", "collect"],
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> RehostServerEvidence | None:
    """Execute one bounded SSM action and return only compact evidence."""
    payload = build_remote_action_payload(action, point)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="trackrelay-workload-ssm-",
        suffix=".json",
    ) as payload_file:
        payload_file.write(json.dumps(payload))
        payload_file.flush()
        command_id = invoke(
            runner,
            (
                *aws_prefix(session),
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--comment",
                f"TrackRelay rehost workload {action}",
                "--parameters",
                f"file://{payload_file.name}",
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ),
            action=f"SSM workload {action}",
        ).stdout.strip()
    if COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise AwsRehostError("SSM returned an invalid workload command ID")
    invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "wait",
            "command-executed",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
        ),
        action=f"SSM workload {action} completion",
    )
    status = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "[Status,ResponseCode]",
            "--output",
            "text",
        ),
        action=f"SSM workload {action} status",
    ).stdout.split()
    if status != ["Success", "0"]:
        raise AwsRehostError(f"SSM workload {action} did not report success")
    if action == "prepare":
        return None

    output = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "StandardOutputContent",
            "--output",
            "text",
        ),
        action="SSM workload evidence collection",
    ).stdout
    evidence_lines = [
        line.removeprefix(EVIDENCE_PREFIX)
        for line in output.splitlines()
        if line.startswith(EVIDENCE_PREFIX)
    ]
    if len(evidence_lines) != 1:
        raise AwsRehostError("SSM returned ambiguous workload evidence")
    try:
        evidence_json = b64decode(
            evidence_lines[0],
            validate=True,
        ).decode("utf-8")
        evidence = RehostServerEvidence.model_validate_json(evidence_json)
    except (ValueError, UnicodeDecodeError) as error:
        raise AwsRehostError("SSM returned invalid workload evidence") from error
    if evidence.point != point:
        raise AwsRehostError("SSM evidence identity differs from the workload")
    return evidence


def execute_local_load(
    command: Sequence[str],
    client: httpx.Client,
    sample_interval_seconds: float,
    summary_path: Path,
    *,
    on_load_started: Callable[[], None] = lambda: None,
    on_load_ended: Callable[[], None] = lambda: None,
) -> tuple[int, tuple[RuntimeMetricsSnapshot, ...], dict[str, object]]:
    """Run k6 locally while sampling the remote API process."""
    exit_code, samples = run_k6_with_resource_sampling(
        command,
        trackrelay_client=client,
        sample_interval_seconds=sample_interval_seconds,
        on_load_started=on_load_started,
        on_load_ended=on_load_ended,
    )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AwsRehostError("k6 did not produce a valid summary") from error
    if not isinstance(summary, dict):
        raise AwsRehostError("k6 summary must be a JSON object")
    return exit_code, samples, summary


def derive_rehost_summary(
    rate_results: Sequence[LegacyBaselineRateResult],
    *,
    completed_at: datetime | None = None,
) -> RehostWorkloadSummary:
    """Apply the same consecutive-pass capacity rule as the local baseline."""
    legacy_summary = derive_legacy_baseline_summary(
        rate_results,
        completed_at=completed_at,
    )
    return RehostWorkloadSummary(
        completed_at=legacy_summary.completed_at,
        maximum_sustainable_rate_per_second=(
            legacy_summary.maximum_sustainable_rate_per_second
        ),
        first_failing_rate_per_second=(
            legacy_summary.first_failing_rate_per_second
        ),
        capacity_is_at_least_highest_tested_rate=(
            legacy_summary.capacity_is_at_least_highest_tested_rate
        ),
        rate_results=legacy_summary.rate_results,
    )


def execute_rehost_workload(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    load_executor: LocalLoadExecutor = execute_local_load,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RehostWorkloadSummary:
    """Run all frozen points from outside AWS and save portable evidence."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "rehost_deployed":
        raise AwsRehostError("the synchronous rehost has not been deployed")
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
        raise AwsRehostError("Terraform returned an invalid public IPv4") from error

    output_root = session.evidence_dir / "rehost-workload"
    output_root.mkdir(parents=True, exist_ok=False)
    definition = frozen_rehost_definition(
        revision=revision,
        region=session.region,
    )
    definition_path = output_root / "benchmark-definition.json"
    _write_model(definition, definition_path)
    rate_results = []
    api_url = f"http://{public_ip}:8000"
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        for rate in definition.offered_rates_per_second:
            test_run_id = uuid4()
            point = RehostWorkloadPoint(
                test_run_id=test_run_id,
                request_rate_per_second=rate,
                duration_seconds=definition.tier_duration_seconds,
                random_seed=definition.random_seed,
                partner_id=definition.partner_id,
                start_at=definition.start_at,
                post_load_settle_timeout_seconds=(
                    definition.post_load_settle_timeout_seconds
                ),
                post_load_stable_window_seconds=(
                    definition.post_load_stable_window_seconds
                ),
            )
            run_directory = output_root / "rates" / str(rate) / str(test_run_id)
            run_directory.mkdir(parents=True)
            manifest_path = run_directory / "input-manifest.json"
            k6_summary_path = run_directory / "k6-summary.json"
            runtime_samples_path = run_directory / "runtime-metrics-samples.json"
            deployment_runtime_path = (
                run_directory / "deployment-runtime-timeline.json"
            )
            server_evidence_path = run_directory / "server-evidence.json"
            rate_result_path = run_directory / "rate-result.json"
            write_input_manifest(point.manifest(), manifest_path)

            run_remote_action(
                session,
                instance_id=instance_id,
                action="prepare",
                point=point,
                runner=runner,
            )
            configuration = PerformanceExperimentConfiguration(
                scenario="healthy",
                request_rate_per_second=rate,
                duration_seconds=definition.tier_duration_seconds,
                random_seed=definition.random_seed,
                partner_id=definition.partner_id,
                start_at=definition.start_at,
                resource_sample_interval_seconds=(
                    definition.resource_sample_interval_seconds
                ),
                post_load_settle_timeout_seconds=(
                    definition.post_load_settle_timeout_seconds
                ),
                post_load_stable_window_seconds=(
                    definition.post_load_stable_window_seconds
                ),
                k6_image=definition.k6_image,
                trackrelay_api_url=api_url,
                trackrelay_api_url_for_container=api_url,
            )
            command = build_k6_command(
                configuration,
                manifest_path=manifest_path,
                run_directory=run_directory,
            )
            runtime_command_id = start_remote_runtime_sampling(
                session,
                instance_id=instance_id,
                point=point,
                runner=runner,
            )
            try:
                k6_exit_code, samples, k6_summary = load_executor(
                    command,
                    client,
                    definition.resource_sample_interval_seconds,
                    k6_summary_path,
                )
            finally:
                runtime_timeline = collect_remote_runtime_sampling(
                    session,
                    instance_id=instance_id,
                    command_id=runtime_command_id,
                    point=point,
                    runner=runner,
                )
            server_evidence = run_remote_action(
                session,
                instance_id=instance_id,
                action="collect",
                point=point,
                runner=runner,
            )
            if server_evidence is None:
                raise AwsRehostError("SSM collection returned no evidence")

            _write_model(
                RuntimeMetricsSamples(
                    test_run_id=test_run_id,
                    samples=samples,
                ),
                runtime_samples_path,
            )
            _write_model(runtime_timeline, deployment_runtime_path)
            _write_model(server_evidence, server_evidence_path)
            if not k6_summary_path.exists():
                k6_summary_path.write_text(
                    f"{json.dumps(k6_summary, indent=2)}\n",
                    encoding="utf-8",
                )
            performance_result = derive_performance_result(
                configuration,
                test_run_id=test_run_id,
                k6_exit_code=k6_exit_code,
                k6_summary=k6_summary,
                resource_samples=samples,
                reconciliation=server_evidence.reconciliation,
            )
            compact_result = compact_rate_result(
                performance_result,
                raw_evidence_directory=run_directory.relative_to(
                    session.evidence_dir
                ),
            )
            _write_model(compact_result, rate_result_path)
            rate_results.append(compact_result)

    summary = derive_rehost_summary(rate_results, completed_at=now())
    summary_path = output_root / "summary.json"
    _write_model(summary, summary_path)
    manifest.update(
        {
            "rehost_workload": {
                "benchmark_definition_sha256": file_sha256(definition_path),
                "completed_at": summary.completed_at.isoformat(),
                "result": "rehost-workload/summary.json",
            },
            "status": "rehost_workload_collected",
        }
    )
    write_manifest(session, manifest)
    return summary


def workload_from_arguments(arguments: Namespace) -> RehostWorkloadSummary:
    """Build the shared AWS session object and execute the workload."""
    try:
        return execute_rehost_workload(session_from_arguments(arguments))
    except AwsRehostError:
        raise
    except (httpx.HTTPError, OSError, ValueError) as error:
        raise AwsRehostError(
            "workload execution failed; inspect the private session evidence"
        ) from error
