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
from shlex import quote
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Annotated, Literal
from uuid import UUID, uuid4

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
    run_ssm_payload,
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
    RUNTIME_READY_PREFIX,
    RUNTIME_SAMPLING_TIMEOUT_MARGIN_SECONDS,
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
    ...,
    tuple[int, tuple[RuntimeMetricsSnapshot, ...], dict[str, object]],
]
RUNTIME_SAMPLER_COLLECTION_MAX_WAIT_SECONDS = 120
RUNTIME_SAMPLER_COLLECTION_EXECUTION_TIMEOUT_SECONDS = 150
RUNTIME_SAMPLER_STOP_FILE = "/tmp/trackrelay-load-complete"
SSM_COMMAND_DELIVERY_TIMEOUT_SECONDS = 30
SSM_RECOVERY_MAX_PROBES = 8
SSM_RECOVERY_RETRY_INTERVAL_SECONDS = 5
SSM_COLLECTION_MAX_ATTEMPTS = 3
RETRYABLE_SSM_DELIVERY_STATUSES = frozenset(
    {"DeliveryTimedOut", "Undeliverable"}
)


class RemoteRuntimeSampler(BaseModel):
    """A detached private sampler proven ready before load begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    container_name: str = Field(
        pattern=r"^trackrelay-runtime-[0-9a-f]{32}$"
    )
    ready_at: AwareDatetime


class SsmWorkloadCommandAttempt(BaseModel):
    """One submitted SSM command and its last observed delivery outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["prepare", "recovery-probe", "collect"]
    command_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    status: str
    response_code: int | None = None


class SsmWorkloadCommandEvidence(BaseModel):
    """Every SSM attempt made for one workload action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    action: Literal["prepare", "collect"]
    attempts: tuple[SsmWorkloadCommandAttempt, ...]


def _ssm_command_was_never_delivered(
    attempt: SsmWorkloadCommandAttempt,
) -> bool:
    return (
        attempt.status in RETRYABLE_SSM_DELIVERY_STATUSES
        and attempt.response_code == -1
    )


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


def runtime_sampler_container_name(point: RehostWorkloadPoint) -> str:
    """Derive one narrow Docker identity from the synthetic run UUID."""
    return f"trackrelay-runtime-{point.test_run_id.hex}"


def _runtime_sampling_command(point: RehostWorkloadPoint) -> str:
    sampling_timeout = (
        point.duration_seconds + RUNTIME_SAMPLING_TIMEOUT_MARGIN_SECONDS
    )
    return " ".join(
        (
            "python -m trackrelay.experiments.rehost",
            "sample-runtime",
            f"--test-run-id {point.test_run_id}",
            f"--rate {point.request_rate_per_second}",
            f"--duration-seconds {point.duration_seconds}",
            f"--sampling-timeout-seconds {sampling_timeout}",
            f"--stop-file {RUNTIME_SAMPLER_STOP_FILE}",
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


def build_runtime_sampling_start_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Start a detached sampler and return only after its first reads."""
    container_name = runtime_sampler_container_name(point)
    compose_run = " ".join(
        (
            "docker compose",
            "--project-name trackrelay-rehost",
            "--env-file /opt/trackrelay/.env",
            "--file /opt/trackrelay/compose.yaml",
            'run --detach --no-deps --name "$container_name" api',
            _runtime_sampling_command(point),
        )
    )
    command = "\n".join(
        (
            "set -euo pipefail",
            f"container_name={quote(container_name)}",
            "cleanup_failed_start() {",
            "  exit_code=$?",
            "  trap - EXIT",
            '  docker rm --force "$container_name" >/dev/null 2>&1 || true',
            '  exit "$exit_code"',
            "}",
            "trap cleanup_failed_start EXIT",
            'docker rm --force "$container_name" >/dev/null 2>&1 || true',
            f"{compose_run} >/dev/null",
            "attempt=0",
            'while [ "$attempt" -lt 60 ]; do',
            (
                '  ready_line="$(docker logs "$container_name" 2>/dev/null '
                f"| grep '^{RUNTIME_READY_PREFIX}' | tail -n 1 || true)\""
            ),
            '  if [ -n "$ready_line" ]; then',
            "    printf '%s\\n' \"$ready_line\"",
            "    trap - EXIT",
            "    exit 0",
            "  fi",
            (
                "  running=\"$(docker inspect --format "
                "'{{.State.Running}}' \"$container_name\" 2>/dev/null "
                "|| true)\""
            ),
            '  if [ "$running" != "true" ]; then exit 1; fi',
            "  attempt=$((attempt + 1))",
            "  sleep 1",
            "done",
            "exit 1",
        )
    )
    payload = {
        "commands": [command],
        "executionTimeout": ["90"],
    }
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError(
            "SSM runtime-sampling start payload exceeds the safety limit"
        )
    return payload


def build_runtime_sampling_collect_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Wait for one detached sampler, return its logs, and remove it."""
    container_name = runtime_sampler_container_name(point)
    command = "\n".join(
        (
            "set -euo pipefail",
            f"container_name={quote(container_name)}",
            "cleanup_sampler() {",
            "  exit_code=$?",
            "  trap - EXIT",
            '  docker rm --force "$container_name" >/dev/null 2>&1 || true',
            '  exit "$exit_code"',
            "}",
            "trap cleanup_sampler EXIT",
            "attempt=0",
            (
                'while [ "$attempt" -lt '
                f'{RUNTIME_SAMPLER_COLLECTION_MAX_WAIT_SECONDS} ]; do'
            ),
            (
                "  running=\"$(docker inspect --format "
                "'{{.State.Running}}' \"$container_name\" 2>/dev/null "
                "|| true)\""
            ),
            '  if [ "$running" != "true" ]; then break; fi',
            "  attempt=$((attempt + 1))",
            "  sleep 1",
            "done",
            (
                "running=\"$(docker inspect --format '{{.State.Running}}' "
                '"$container_name" 2>/dev/null || true)"'
            ),
            'if [ "$running" = "true" ]; then exit 1; fi',
            (
                "exit_code=\"$(docker inspect --format '{{.State.ExitCode}}' "
                '"$container_name")"'
            ),
            'docker logs "$container_name"',
            'test "$exit_code" = "0"',
        )
    )
    payload = {
        "commands": [command],
        "executionTimeout": [
            str(RUNTIME_SAMPLER_COLLECTION_EXECUTION_TIMEOUT_SECONDS)
        ],
    }
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError(
            "SSM runtime-sampling collect payload exceeds the safety limit"
        )
    return payload


def build_runtime_sampling_stop_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Tell a live sampler to take its final observation and stop."""
    container_name = runtime_sampler_container_name(point)
    command = "\n".join(
        (
            "set -euo pipefail",
            f"container_name={quote(container_name)}",
            (
                'docker exec "$container_name" sh -c '
                f"{quote(f'umask 077; : > {RUNTIME_SAMPLER_STOP_FILE}')}"
            ),
        )
    )
    return {
        "commands": [command],
        "executionTimeout": ["30"],
    }


def build_runtime_sampling_cleanup_payload(
    point: RehostWorkloadPoint,
) -> dict[str, list[str]]:
    """Build an idempotent orphan-sampler cleanup command."""
    container_name = runtime_sampler_container_name(point)
    return {
        "commands": [
            (
                "set -euo pipefail\n"
                f"docker rm --force {quote(container_name)} "
                ">/dev/null 2>&1 || true"
            )
        ],
        "executionTimeout": ["30"],
    }


def start_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> RemoteRuntimeSampler:
    """Start a detached sampler through one completed, bounded SSM command."""
    container_name = runtime_sampler_container_name(point)
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_runtime_sampling_start_payload(point),
        comment="TrackRelay detached runtime sampler start",
        runner=runner,
    )
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
        action="SSM detached runtime sampler readiness",
    ).stdout
    ready_lines = [
        line.removeprefix(RUNTIME_READY_PREFIX)
        for line in output.splitlines()
        if line.startswith(RUNTIME_READY_PREFIX)
    ]
    try:
        if len(ready_lines) != 1:
            raise ValueError("sampler returned ambiguous readiness")
        ready_at = datetime.fromisoformat(ready_lines[0])
        if ready_at.utcoffset() is None:
            raise ValueError("sampler readiness timestamp is naive")
    except ValueError as error:
        cleanup_remote_runtime_sampling(
            session,
            instance_id=instance_id,
            point=point,
            runner=runner,
        )
        raise AwsRehostError(
            "SSM returned invalid detached sampler readiness"
        ) from error
    return RemoteRuntimeSampler(
        container_name=container_name,
        ready_at=ready_at,
    )


def collect_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    sampler: RemoteRuntimeSampler,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> RehostRuntimeTimeline:
    """Collect one detached sampler through a second bounded SSM command."""
    if sampler.container_name != runtime_sampler_container_name(point):
        raise AwsRehostError("runtime sampler identity differs from the workload")
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_runtime_sampling_collect_payload(point),
        comment="TrackRelay detached runtime sampler collection",
        runner=runner,
    )
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
    if timeline.coverage_started_at != sampler.ready_at:
        raise AwsRehostError(
            "runtime evidence start differs from sampler readiness"
        )
    return timeline


def stop_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    sampler: RemoteRuntimeSampler,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> None:
    """Signal the matching live sampler immediately after k6 exits."""
    if sampler.container_name != runtime_sampler_container_name(point):
        raise AwsRehostError("runtime sampler identity differs from the workload")
    run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_runtime_sampling_stop_payload(point),
        comment="TrackRelay detached runtime sampler stop",
        runner=runner,
    )


def cleanup_remote_runtime_sampling(
    session: AwsSession,
    *,
    instance_id: str,
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
) -> None:
    """Idempotently remove a detached sampler after an interrupted load."""
    run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_runtime_sampling_cleanup_payload(point),
        comment="TrackRelay detached runtime sampler cleanup",
        runner=runner,
    )


def validate_runtime_timeline_covers_load(
    timeline: RehostRuntimeTimeline,
    *,
    test_run_id: UUID,
    load_started_at: datetime,
    load_ended_at: datetime,
) -> None:
    """Reject process evidence that does not contain the whole load window."""
    if timeline.test_run_id != test_run_id:
        raise AwsRehostError("runtime timeline identity differs from load window")
    if load_ended_at <= load_started_at:
        raise AwsRehostError("load window must have positive duration")
    if timeline.coverage_started_at > load_started_at:
        raise AwsRehostError("runtime sampling began after the load")
    if timeline.coverage_ended_at < load_ended_at:
        raise AwsRehostError("runtime sampling ended before the load")


def _submit_workload_ssm_command(
    session: AwsSession,
    *,
    instance_id: str,
    payload: dict[str, list[str]],
    comment: str,
    runner: ProcessRunner,
) -> str:
    """Submit one workload-related command with a bounded delivery window."""
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
                comment,
                "--parameters",
                f"file://{payload_file.name}",
                "--timeout-seconds",
                str(SSM_COMMAND_DELIVERY_TIMEOUT_SECONDS),
                "--query",
                "Command.CommandId",
                "--output",
                "text",
            ),
            action="SSM workload command submission",
        ).stdout.strip()
    if COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise AwsRehostError("SSM returned an invalid workload command ID")
    return command_id


def _wait_for_workload_ssm_command(
    session: AwsSession,
    *,
    instance_id: str,
    command_id: str,
    runner: ProcessRunner,
) -> tuple[str, int]:
    """Observe the terminal invocation even when the AWS waiter returns nonzero."""
    runner(
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
        None,
    )
    invocation = invoke(
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
        action="SSM workload command status",
    ).stdout.split()
    if len(invocation) != 2:
        raise AwsRehostError("SSM workload command returned an invalid status")
    try:
        response_code = int(invocation[1])
    except ValueError as error:
        raise AwsRehostError(
            "SSM workload command returned an invalid response code"
        ) from error
    return invocation[0], response_code


def _write_ssm_workload_attempts(
    path: Path | None,
    *,
    point: RehostWorkloadPoint,
    action: Literal["prepare", "collect"],
    attempts: Sequence[SsmWorkloadCommandAttempt],
) -> None:
    if path is None:
        return
    _write_model(
        SsmWorkloadCommandEvidence(
            test_run_id=point.test_run_id,
            action=action,
            attempts=tuple(attempts),
        ),
        path,
    )


def _execute_workload_ssm_attempt(
    session: AwsSession,
    *,
    instance_id: str,
    payload: dict[str, list[str]],
    comment: str,
    purpose: Literal["prepare", "recovery-probe", "collect"],
    point: RehostWorkloadPoint,
    action: Literal["prepare", "collect"],
    attempts: list[SsmWorkloadCommandAttempt],
    attempt_evidence_path: Path | None,
    runner: ProcessRunner,
) -> SsmWorkloadCommandAttempt:
    """Submit, journal, and observe one command without hiding delivery failure."""
    command_id = _submit_workload_ssm_command(
        session,
        instance_id=instance_id,
        payload=payload,
        comment=comment,
        runner=runner,
    )
    submitted = SsmWorkloadCommandAttempt(
        purpose=purpose,
        command_id=command_id,
        status="Submitted",
    )
    attempts.append(submitted)
    _write_ssm_workload_attempts(
        attempt_evidence_path,
        point=point,
        action=action,
        attempts=attempts,
    )
    status, response_code = _wait_for_workload_ssm_command(
        session,
        instance_id=instance_id,
        command_id=command_id,
        runner=runner,
    )
    completed = submitted.model_copy(
        update={"status": status, "response_code": response_code}
    )
    attempts[-1] = completed
    _write_ssm_workload_attempts(
        attempt_evidence_path,
        point=point,
        action=action,
        attempts=attempts,
    )
    return completed


def _wait_for_post_load_ssm_recovery(
    session: AwsSession,
    *,
    instance_id: str,
    point: RehostWorkloadPoint,
    attempts: list[SsmWorkloadCommandAttempt],
    attempt_evidence_path: Path | None,
    runner: ProcessRunner,
    sleeper: Callable[[float], None],
) -> None:
    """Require the overloaded host to execute a harmless command before collect."""
    probe_payload = {
        "commands": ["set -euo pipefail\ntrue"],
        "executionTimeout": ["10"],
    }
    for probe_number in range(SSM_RECOVERY_MAX_PROBES):
        attempt = _execute_workload_ssm_attempt(
            session,
            instance_id=instance_id,
            payload=probe_payload,
            comment="TrackRelay post-load SSM recovery probe",
            purpose="recovery-probe",
            point=point,
            action="collect",
            attempts=attempts,
            attempt_evidence_path=attempt_evidence_path,
            runner=runner,
        )
        if (attempt.status, attempt.response_code) == ("Success", 0):
            return
        if not _ssm_command_was_never_delivered(attempt):
            raise AwsRehostError(
                "post-load SSM recovery probe did not succeed safely"
            )
        if probe_number < SSM_RECOVERY_MAX_PROBES - 1:
            sleeper(SSM_RECOVERY_RETRY_INTERVAL_SECONDS)
    raise AwsRehostError("post-load SSM recovery gate timed out")


def run_remote_action(
    session: AwsSession,
    *,
    instance_id: str,
    action: Literal["prepare", "collect"],
    point: RehostWorkloadPoint,
    runner: ProcessRunner,
    attempt_evidence_path: Path | None = None,
    sleeper: Callable[[float], None] = sleep,
) -> RehostServerEvidence | None:
    """Execute one delivery-aware SSM action and return compact evidence."""
    attempts: list[SsmWorkloadCommandAttempt] = []
    payload = build_remote_action_payload(action, point)
    if action == "prepare":
        attempt = _execute_workload_ssm_attempt(
            session,
            instance_id=instance_id,
            payload=payload,
            comment="TrackRelay rehost workload prepare",
            purpose="prepare",
            point=point,
            action=action,
            attempts=attempts,
            attempt_evidence_path=attempt_evidence_path,
            runner=runner,
        )
        if (attempt.status, attempt.response_code) != ("Success", 0):
            raise AwsRehostError("SSM workload prepare did not report success")
        return None

    command_id = ""
    for collection_number in range(SSM_COLLECTION_MAX_ATTEMPTS):
        _wait_for_post_load_ssm_recovery(
            session,
            instance_id=instance_id,
            point=point,
            attempts=attempts,
            attempt_evidence_path=attempt_evidence_path,
            runner=runner,
            sleeper=sleeper,
        )
        attempt = _execute_workload_ssm_attempt(
            session,
            instance_id=instance_id,
            payload=payload,
            comment="TrackRelay rehost workload collect",
            purpose="collect",
            point=point,
            action=action,
            attempts=attempts,
            attempt_evidence_path=attempt_evidence_path,
            runner=runner,
        )
        command_id = attempt.command_id
        if (attempt.status, attempt.response_code) == ("Success", 0):
            break
        if not _ssm_command_was_never_delivered(attempt):
            raise AwsRehostError(
                "SSM workload collect did not succeed and was not proven "
                "undelivered"
            )
        if collection_number == SSM_COLLECTION_MAX_ATTEMPTS - 1:
            raise AwsRehostError(
                "SSM workload collect exhausted safe delivery retries"
            )

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
    runtime_stopper: Callable[..., None] = stop_remote_runtime_sampling,
    runtime_cleaner: Callable[..., None] = cleanup_remote_runtime_sampling,
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
            prepare_attempts_path = (
                run_directory / "ssm-prepare-command-attempts.json"
            )
            collect_attempts_path = (
                run_directory / "ssm-collect-command-attempts.json"
            )
            write_input_manifest(point.manifest(), manifest_path)

            run_remote_action(
                session,
                instance_id=instance_id,
                action="prepare",
                point=point,
                runner=runner,
                attempt_evidence_path=prepare_attempts_path,
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
            runtime_sampler = start_remote_runtime_sampling(
                session,
                instance_id=instance_id,
                point=point,
                runner=runner,
            )
            load_boundaries: list[datetime] = []

            def record_load_started(
                boundaries: list[datetime] = load_boundaries,
            ) -> None:
                boundaries.append(now())

            def record_load_ended(
                boundaries: list[datetime] = load_boundaries,
                sampler: RemoteRuntimeSampler = runtime_sampler,
                active_point: RehostWorkloadPoint = point,
            ) -> None:
                boundaries.append(now())
                runtime_stopper(
                    session,
                    instance_id=instance_id,
                    sampler=sampler,
                    point=active_point,
                    runner=runner,
                )

            try:
                k6_exit_code, samples, k6_summary = load_executor(
                    command,
                    client,
                    definition.resource_sample_interval_seconds,
                    k6_summary_path,
                    on_load_started=record_load_started,
                    on_load_ended=record_load_ended,
                )
                if len(load_boundaries) != 2:
                    raise AwsRehostError(
                        "load executor did not report exact process boundaries"
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
            runtime_timeline = collect_remote_runtime_sampling(
                session,
                instance_id=instance_id,
                sampler=runtime_sampler,
                point=point,
                runner=runner,
            )
            if not samples:
                raise AwsRehostError("local runtime sampling returned no samples")
            validate_runtime_timeline_covers_load(
                runtime_timeline,
                test_run_id=test_run_id,
                load_started_at=load_boundaries[0],
                load_ended_at=load_boundaries[1],
            )
            server_evidence = run_remote_action(
                session,
                instance_id=instance_id,
                action="collect",
                point=point,
                runner=runner,
                attempt_evidence_path=collect_attempts_path,
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
