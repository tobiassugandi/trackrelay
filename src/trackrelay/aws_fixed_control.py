"""Run the Stage 9.6 fixed-worker control and retain aligned evidence."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from itertools import pairwise
from json import JSONDecodeError, dumps, loads
from pathlib import Path
from signal import SIGTERM, getsignal, signal
from subprocess import DEVNULL, STDOUT, Popen, TimeoutExpired
from time import sleep
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from trackrelay.aws_async_deployment import (
    CLUSTER_NAME_PATTERN,
    FIXED_SERVICE_CAPACITY,
    SERVICE_NAME_PATTERN,
    AwsAsyncDeploymentError,
    ProcessRunner,
    aws_prefix,
    invoke,
    require_clean_approved_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_async_integration import AsyncRunSummary
from trackrelay.aws_elasticity_cloudwatch import (
    AwsElasticityCloudWatchError,
    ElasticityCloudWatchEvidence,
    collect_elasticity_cloudwatch_evidence,
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
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    ElasticityWorkloadDefinition,
    build_elasticity_k6_command,
    build_elasticity_manifest,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
PositiveInteger = Annotated[int, Field(gt=0)]
Sleeper = Callable[[float], None]
Now = Callable[[], datetime]
SessionAction = Callable[..., object]
TreatmentRunner = Callable[..., "FixedControlResult"]
MetricCollector = Callable[..., ElasticityCloudWatchEvidence]


class AwsFixedControlError(RuntimeError):
    """A safe, actionable Stage 9.6 fixed-control failure."""


class AwsFixedControlCleanupError(RuntimeError):
    """Report cleanup failures without hiding the fixed-control failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException,
        cleanup_errors: Sequence[BaseException],
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


class AwsFixedControlQualificationError(AwsFixedControlError):
    """The measured candidate cannot proceed to an elastic replay."""


class FixedControlObservationFailure(BaseModel):
    """One sanitized gap retained instead of aborting overload observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    seconds_after_load_started: NonNegativeFloat
    phase: Literal["during-load", "post-load"]
    kind: Literal["api-summary", "queue", "ecs", "invalid-response"]


class FixedControlObservation(BaseModel):
    """One aligned application, queue, and worker-capacity observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    seconds_after_load_started: NonNegativeFloat
    phase: Literal["during-load", "post-load"]
    step_name: str
    offered_rate_per_second: NonNegativeInteger
    database_persisted_events: NonNegativeInteger
    database_processed_events: NonNegativeInteger
    database_failed_events: NonNegativeInteger
    database_pending_events: NonNegativeInteger
    completed_delivery_events: NonNegativeInteger
    durable_outbox_entries: NonNegativeInteger
    pending_outbox_entries: NonNegativeInteger
    source_queue_visible_messages: NonNegativeInteger
    source_queue_in_flight_messages: NonNegativeInteger
    source_queue_delayed_messages: NonNegativeInteger
    dead_letter_queue_messages: NonNegativeInteger
    worker_desired_count: NonNegativeInteger
    worker_running_count: NonNegativeInteger
    worker_pending_count: NonNegativeInteger
    api_runtime: RuntimeMetricsSnapshot | None = None

    @computed_field
    @property
    def source_queue_work(self) -> int:
        return (
            self.source_queue_visible_messages
            + self.source_queue_in_flight_messages
            + self.source_queue_delayed_messages
        )

    @computed_field
    @property
    def processing_drained(self) -> bool:
        return (
            self.database_persisted_events == self.completed_delivery_events
            and self.durable_outbox_entries == self.database_persisted_events
            and self.pending_outbox_entries == 0
            and self.source_queue_work == 0
            and self.dead_letter_queue_messages == 0
        )


class FixedControlIngestionStepResult(BaseModel):
    """Driver-observed ingestion result for one scheduled workload plateau."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_name: str
    offered_rate_per_second: PositiveInteger
    duration_seconds: PositiveInteger
    expected_request_count: PositiveInteger
    observed_request_count: NonNegativeInteger
    p95_response_latency_ms: NonNegativeFloat
    request_error_percent: Annotated[float, Field(ge=0, le=100)]

    @computed_field
    @property
    def ingestion_guardrails_passed(self) -> bool:
        return (
            self.observed_request_count == self.expected_request_count
            and self.p95_response_latency_ms < 500
            and self.request_error_percent < 1
        )


class ElasticityResult(BaseModel):
    """Shared measurement contract for both causal treatments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    definition: ElasticityWorkloadDefinition
    load_started_at: AwareDatetime
    load_ended_at: AwareDatetime
    k6_exit_code: int
    dropped_iteration_count: NonNegativeInteger
    ingestion_steps: tuple[FixedControlIngestionStepResult, ...]
    observations: tuple[FixedControlObservation, ...]
    observation_failures: tuple[FixedControlObservationFailure, ...]
    drain_stability_confirmed: bool
    reconciliation: ReconciliationReport

    @model_validator(mode="after")
    def require_consistent_fixed_control(self) -> "ElasticityResult":
        if self.load_ended_at <= self.load_started_at:
            raise ValueError("fixed-control load window must be positive")
        if tuple(item.step_name for item in self.ingestion_steps) != tuple(
            step.name for step in self.definition.steps
        ):
            raise ValueError("fixed-control ingestion steps differ from definition")
        if any(
            later.observed_at <= earlier.observed_at
            for earlier, later in pairwise(self.observations)
        ):
            raise ValueError("fixed-control observations must be ordered")
        if self.reconciliation.test_run_id != self.test_run_id:
            raise ValueError("fixed-control reconciliation uses another run")
        return self

    @computed_field
    @property
    def ingestion_guardrails_passed(self) -> bool:
        return (
            self.k6_exit_code == 0
            and self.dropped_iteration_count == 0
            and all(step.ingestion_guardrails_passed for step in self.ingestion_steps)
        )

    @computed_field
    @property
    def fixed_worker_contract_preserved(self) -> bool:
        return bool(self.observations) and all(
            observation.worker_desired_count == 1
            and observation.worker_running_count == 1
            and observation.worker_pending_count == 0
            for observation in self.observations
        )

    @computed_field
    @property
    def maximum_source_queue_work(self) -> int:
        return max(
            (observation.source_queue_work for observation in self.observations),
            default=0,
        )

    @computed_field
    @property
    def worker_pressure_observed(self) -> bool:
        peak_names = {
            step.name
            for step in self.definition.steps
            if step.offered_rate_per_second == self.definition.peak_rate_per_second
        }
        return any(
            observation.step_name in peak_names and observation.source_queue_work > 0
            for observation in self.observations
        )

    @computed_field
    @property
    def correctness_guardrails_passed(self) -> bool:
        report = self.reconciliation
        return (
            report.generated == self.definition.expected_request_count
            and report.accepted == report.generated
            and report.unique == report.generated
            and report.processed == report.generated
            and report.failed == 0
            and report.pending == 0
            and report.unaccounted == 0
            and report.simulator_unique_events == report.generated
            and report.duplicate_business_effects == 0
            and report.incorrect_final_shipment_states == 0
            and report.invariants_passed
        )

    @computed_field
    @property
    def processing_drained(self) -> bool:
        return bool(self.observations) and self.observations[-1].processing_drained

    @computed_field
    @property
    def measurement_complete(self) -> bool:
        return (
            self.ingestion_guardrails_passed
            and self.correctness_guardrails_passed
            and self.processing_drained
            and self.drain_stability_confirmed
            and not self.observation_failures
        )


class FixedControlResult(ElasticityResult):
    """The shared measurement additionally requires a fixed one-worker tier."""

    @computed_field
    @property
    def measurement_complete(self) -> bool:
        return super().measurement_complete and self.fixed_worker_contract_preserved


class ElasticityGuardrailQualification(BaseModel):
    """Shared ingestion, correctness, evidence, and non-worker headroom gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    native_request_count: NonNegativeFloat
    native_request_error_percent: Annotated[float, Field(ge=0, le=100)]
    maximum_native_p95_latency_ms: NonNegativeFloat
    maximum_api_cpu_percent: NonNegativeFloat
    maximum_api_memory_percent: NonNegativeFloat
    maximum_simulator_cpu_percent: NonNegativeFloat
    maximum_simulator_memory_percent: NonNegativeFloat
    minimum_worker_running_tasks: NonNegativeFloat
    maximum_worker_running_tasks: NonNegativeFloat
    maximum_worker_cpu_percent: NonNegativeFloat
    maximum_dead_letter_queue_messages: NonNegativeFloat
    maximum_rds_cpu_percent: NonNegativeFloat
    maximum_rds_connections: NonNegativeFloat
    minimum_rds_freeable_memory_bytes: NonNegativeFloat
    maximum_rds_read_latency_seconds: NonNegativeFloat
    maximum_rds_write_latency_seconds: NonNegativeFloat
    maximum_rds_read_iops: NonNegativeFloat
    maximum_rds_write_iops: NonNegativeFloat
    maximum_database_pool_utilization_percent: NonNegativeFloat | None
    database_pool_steps_observed: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    qualified: bool

    @model_validator(mode="after")
    def require_consistent_decision(self) -> "ElasticityGuardrailQualification":
        if self.qualified is bool(self.rejection_reasons):
            raise ValueError("fixed-control qualification disagrees with reasons")
        return self


class FixedControlQualification(ElasticityGuardrailQualification):
    """The candidate additionally requires fixed capacity and worker pressure."""


class FixedControlSummary(BaseModel):
    """Measurement, native evidence, and the resulting candidate decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    measurement: FixedControlResult
    cloudwatch: ElasticityCloudWatchEvidence
    qualification: FixedControlQualification

    @model_validator(mode="after")
    def require_one_treatment_run(self) -> "FixedControlSummary":
        run_ids = {
            self.measurement.test_run_id,
            self.cloudwatch.test_run_id,
            self.qualification.test_run_id,
        }
        if len(run_ids) != 1:
            raise ValueError("fixed-control evidence uses different run IDs")
        if (
            self.cloudwatch.window_started_at != self.measurement.load_started_at
            or self.cloudwatch.window_ended_at != self.measurement.load_ended_at
        ):
            raise ValueError("fixed-control CloudWatch window is misaligned")
        return self


def _series_values(
    evidence: ElasticityCloudWatchEvidence,
    query_id: str,
) -> tuple[float, ...]:
    return tuple(point.value for point in evidence.series_by_id()[query_id].datapoints)


def _database_pool_evidence(
    result: ElasticityResult,
) -> tuple[float | None, tuple[str, ...]]:
    utilizations: list[float] = []
    observed_steps: set[str] = set()
    expected_steps = {step.name for step in result.definition.steps}
    for observation in result.observations:
        if (
            observation.step_name not in expected_steps
            or observation.api_runtime is None
        ):
            continue
        pool = observation.api_runtime.database_pool
        if (
            pool is None
            or pool.checked_out is None
            or pool.pool_size is None
            or pool.max_overflow is None
            or pool.pool_size + pool.max_overflow <= 0
        ):
            continue
        observed_steps.add(observation.step_name)
        utilizations.append(
            100 * pool.checked_out / (pool.pool_size + pool.max_overflow)
        )
    return (
        max(utilizations) if utilizations else None,
        tuple(
            step.name for step in result.definition.steps if step.name in observed_steps
        ),
    )


def evaluate_treatment_guardrails(
    result: ElasticityResult,
    cloudwatch: ElasticityCloudWatchEvidence,
) -> ElasticityGuardrailQualification:
    """Apply the identical non-treatment-specific qualification to both runs."""
    if result.test_run_id != cloudwatch.test_run_id:
        raise ValueError("fixed-control and CloudWatch evidence use different runs")
    if (
        result.load_started_at != cloudwatch.window_started_at
        or result.load_ended_at != cloudwatch.window_ended_at
    ):
        raise ValueError("treatment CloudWatch window is misaligned")
    values = {
        query_id: _series_values(cloudwatch, query_id)
        for query_id in cloudwatch.series_by_id()
    }
    load_balancer_error_count = sum(values["alb_elb_4xx"]) + sum(values["alb_elb_5xx"])
    native_request_count = sum(values["alb_requests"]) + load_balancer_error_count
    native_error_count = (
        sum(values["alb_target_4xx"])
        + sum(values["alb_target_5xx"])
        + load_balancer_error_count
    )
    native_error_percent = (
        100 * native_error_count / native_request_count if native_request_count else 100
    )
    maximum_pool_percent, pool_steps = _database_pool_evidence(result)
    definition = result.definition
    reasons: list[str] = []
    if not result.measurement_complete:
        reasons.append("measurement_incomplete")
    worker_values = values["worker_running_tasks"]
    if native_request_count < definition.expected_request_count:
        reasons.append("native_request_count_incomplete")
    maximum_p95_ms = 1000 * max(values["alb_p95_latency"])
    if maximum_p95_ms >= definition.ingestion_p95_limit_ms:
        reasons.append("native_ingestion_latency_failed")
    if native_error_percent >= definition.ingestion_error_limit_percent:
        reasons.append("native_ingestion_errors_failed")
    headroom_ceiling = definition.maximum_non_worker_utilization_percent
    maximum_api_cpu = max(values["api_cpu"])
    maximum_api_memory = max(values["api_memory"])
    maximum_simulator_cpu = max(values["simulator_cpu"])
    maximum_simulator_memory = max(values["simulator_memory"])
    if maximum_api_cpu >= headroom_ceiling:
        reasons.append("api_cpu_headroom_failed")
    if maximum_api_memory >= headroom_ceiling:
        reasons.append("api_memory_headroom_failed")
    if maximum_simulator_cpu >= headroom_ceiling:
        reasons.append("simulator_cpu_headroom_failed")
    if maximum_simulator_memory >= headroom_ceiling:
        reasons.append("simulator_memory_headroom_failed")
    expected_pool_steps = tuple(step.name for step in definition.steps)
    if pool_steps != expected_pool_steps:
        reasons.append("database_pool_evidence_incomplete")
    elif (
        maximum_pool_percent is None
        or maximum_pool_percent >= definition.maximum_database_pool_utilization_percent
    ):
        reasons.append("database_pool_headroom_failed")
    maximum_rds_cpu = max(values["rds_cpu"])
    maximum_rds_connections = max(values["rds_connections"])
    minimum_rds_memory = min(values["rds_freeable_memory"])
    maximum_rds_read_latency = max(values["rds_read_latency"])
    maximum_rds_write_latency = max(values["rds_write_latency"])
    if maximum_rds_cpu >= definition.maximum_rds_cpu_utilization_percent:
        reasons.append("rds_cpu_headroom_failed")
    if maximum_rds_connections >= definition.maximum_rds_connections:
        reasons.append("rds_connection_headroom_failed")
    if minimum_rds_memory <= definition.minimum_rds_freeable_memory_bytes:
        reasons.append("rds_memory_headroom_failed")
    if maximum_rds_read_latency >= definition.maximum_rds_read_latency_seconds:
        reasons.append("rds_read_latency_headroom_failed")
    if maximum_rds_write_latency >= definition.maximum_rds_write_latency_seconds:
        reasons.append("rds_write_latency_headroom_failed")
    maximum_dlq = max(values["dead_letter_queue_visible"])
    if maximum_dlq > 0:
        reasons.append("native_dead_letter_queue_not_empty")
    return ElasticityGuardrailQualification(
        test_run_id=result.test_run_id,
        native_request_count=native_request_count,
        native_request_error_percent=native_error_percent,
        maximum_native_p95_latency_ms=maximum_p95_ms,
        maximum_api_cpu_percent=maximum_api_cpu,
        maximum_api_memory_percent=maximum_api_memory,
        maximum_simulator_cpu_percent=maximum_simulator_cpu,
        maximum_simulator_memory_percent=maximum_simulator_memory,
        minimum_worker_running_tasks=min(worker_values),
        maximum_worker_running_tasks=max(worker_values),
        maximum_worker_cpu_percent=max(values["worker_cpu"]),
        maximum_dead_letter_queue_messages=maximum_dlq,
        maximum_rds_cpu_percent=maximum_rds_cpu,
        maximum_rds_connections=maximum_rds_connections,
        minimum_rds_freeable_memory_bytes=minimum_rds_memory,
        maximum_rds_read_latency_seconds=maximum_rds_read_latency,
        maximum_rds_write_latency_seconds=maximum_rds_write_latency,
        maximum_rds_read_iops=max(values["rds_read_iops"]),
        maximum_rds_write_iops=max(values["rds_write_iops"]),
        maximum_database_pool_utilization_percent=maximum_pool_percent,
        database_pool_steps_observed=pool_steps,
        rejection_reasons=tuple(reasons),
        qualified=not reasons,
    )


def evaluate_fixed_control_qualification(
    result: FixedControlResult,
    cloudwatch: ElasticityCloudWatchEvidence,
) -> FixedControlQualification:
    """Require worker pressure while every frozen non-worker guardrail passes."""
    common = evaluate_treatment_guardrails(result, cloudwatch)
    reasons = list(common.rejection_reasons)
    if not result.worker_pressure_observed:
        reasons.append("worker_pressure_not_observed")
    if any(
        abs(value - result.definition.minimum_worker_count) > 0.01
        for value in _series_values(cloudwatch, "worker_running_tasks")
    ):
        reasons.append("fixed_worker_metric_drift")
    return FixedControlQualification(
        **{
            **common.model_dump(),
            "rejection_reasons": tuple(reasons),
            "qualified": not reasons,
        }
    )


def validate_fixed_control_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> dict[str, object]:
    """Require the exact approved, deployed cloud-session-4 control."""
    if not session.session_id.startswith("cloud-session-4-"):
        raise AwsSessionError("Stage 9.6 fixed control must use cloud session 4")
    if session.deployment_mode != "async":
        raise AwsSessionError("Stage 9.6 fixed control requires async mode")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "async_deployed":
        raise AwsSessionError("fixed control requires the converged async deployment")
    recorded_ceiling = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded_ceiling, str) or parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    ) != parse_positive_money(recorded_ceiling, field_name="recorded cost ceiling"):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    return manifest


def _metric_value(
    summary: Mapping[str, object],
    metric_name: str,
    value_name: str,
) -> float:
    try:
        metrics = summary["metrics"]
        if not isinstance(metrics, Mapping):
            raise TypeError
        metric = metrics[metric_name]
        if not isinstance(metric, Mapping):
            raise TypeError
        values = metric["values"]
        if not isinstance(values, Mapping):
            raise TypeError
        value = float(values[value_name])
    except (KeyError, TypeError, ValueError) as error:
        raise AwsFixedControlError(
            f"k6 summary omitted {metric_name}.{value_name}"
        ) from error
    if value < 0:
        raise AwsFixedControlError(
            f"k6 summary reported negative {metric_name}.{value_name}"
        )
    return value


def derive_ingestion_steps(
    definition: ElasticityWorkloadDefinition,
    k6_summary: Mapping[str, object],
) -> tuple[FixedControlIngestionStepResult, ...]:
    """Derive every plateau from k6's threshold-created tagged submetrics."""
    results = []
    for step in definition.steps:
        tag = f"{{step:{step.name}}}"
        results.append(
            FixedControlIngestionStepResult(
                step_name=step.name,
                offered_rate_per_second=step.offered_rate_per_second,
                duration_seconds=step.duration_seconds,
                expected_request_count=step.expected_request_count,
                observed_request_count=int(
                    _metric_value(k6_summary, f"http_reqs{tag}", "count")
                ),
                p95_response_latency_ms=_metric_value(
                    k6_summary,
                    f"http_req_duration{tag}",
                    "p(95)",
                ),
                request_error_percent=100
                * _metric_value(
                    k6_summary,
                    f"http_req_failed{tag}",
                    "rate",
                ),
            )
        )
    return tuple(results)


def _active_step(
    definition: ElasticityWorkloadDefinition,
    seconds_after_load_started: float,
) -> tuple[str, int]:
    elapsed_boundary = 0
    for step in definition.steps:
        elapsed_boundary += step.duration_seconds
        if seconds_after_load_started < elapsed_boundary:
            return step.name, step.offered_rate_per_second
    return "post-load", 0


def _queue_attributes(
    session: AwsSession,
    queue_url: str,
    *,
    names: Sequence[str],
    runner: ProcessRunner,
) -> dict[str, int]:
    try:
        result = invoke(
            runner,
            (
                *aws_prefix(session),
                "sqs",
                "get-queue-attributes",
                "--queue-url",
                queue_url,
                "--attribute-names",
                *names,
                "--query",
                "Attributes",
                "--output",
                "json",
            ),
            action="SQS fixed-control observation",
        )
    except AwsAsyncDeploymentError as error:
        raise AwsFixedControlError("SQS fixed-control observation failed") from error
    try:
        attributes = loads(result.stdout)
        parsed = {name: int(attributes[name]) for name in names}
    except (JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AwsFixedControlError(
            "SQS returned invalid fixed-control state"
        ) from error
    if any(value < 0 for value in parsed.values()):
        raise AwsFixedControlError("SQS returned negative fixed-control state")
    return parsed


def _worker_counts(
    session: AwsSession,
    *,
    cluster_name: str,
    worker_service_name: str,
    runner: ProcessRunner,
) -> tuple[int, int, int]:
    try:
        result = invoke(
            runner,
            (
                *aws_prefix(session),
                "ecs",
                "describe-services",
                "--cluster",
                cluster_name,
                "--services",
                worker_service_name,
                "--query",
                "services[0].[desiredCount,runningCount,pendingCount]",
                "--output",
                "json",
            ),
            action="ECS fixed-worker observation",
        )
    except AwsAsyncDeploymentError as error:
        raise AwsFixedControlError("ECS fixed-worker observation failed") from error
    try:
        counts = loads(result.stdout)
        desired, running, pending = (int(value) for value in counts)
    except (JSONDecodeError, TypeError, ValueError) as error:
        raise AwsFixedControlError("ECS returned invalid fixed-worker state") from error
    if min(desired, running, pending) < 0:
        raise AwsFixedControlError("ECS returned negative fixed-worker state")
    return desired, running, pending


def collect_fixed_control_observation(
    session: AwsSession,
    *,
    api_client: httpx.Client,
    test_run_id: UUID,
    source_queue_url: str,
    dead_letter_queue_url: str,
    cluster_name: str,
    worker_service_name: str,
    load_started_at: datetime,
    observed_at: datetime,
    load_ended_at: datetime | None,
    definition: ElasticityWorkloadDefinition,
    runner: ProcessRunner = run_process,
) -> FixedControlObservation:
    """Collect one aligned application, queue, worker, and API-pool sample."""
    elapsed = max(0.0, (observed_at - load_started_at).total_seconds())
    step_name, offered_rate = _active_step(definition, elapsed)
    summary_response = api_client.get(f"/api/v1/test-runs/{test_run_id}/summary")
    summary_response.raise_for_status()
    summary = AsyncRunSummary.model_validate(summary_response.json())

    source = _queue_attributes(
        session,
        source_queue_url,
        names=(
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ),
        runner=runner,
    )
    dead_letter = _queue_attributes(
        session,
        dead_letter_queue_url,
        names=("ApproximateNumberOfMessages",),
        runner=runner,
    )
    desired, running, pending = _worker_counts(
        session,
        cluster_name=cluster_name,
        worker_service_name=worker_service_name,
        runner=runner,
    )
    api_runtime: RuntimeMetricsSnapshot | None = None
    try:
        runtime_response = api_client.get("/api/v1/experiments/runtime-metrics")
        runtime_response.raise_for_status()
        api_runtime = RuntimeMetricsSnapshot.model_validate(runtime_response.json())
    except (httpx.HTTPError, ValueError):
        pass
    return FixedControlObservation(
        observed_at=observed_at,
        seconds_after_load_started=elapsed,
        phase="post-load" if load_ended_at is not None else "during-load",
        step_name=step_name,
        offered_rate_per_second=offered_rate,
        database_persisted_events=summary.database_events.persisted,
        database_processed_events=summary.database_events.processed,
        database_failed_events=summary.database_events.failed,
        database_pending_events=summary.database_events.pending,
        completed_delivery_events=(
            summary.database_delivery_attempts.delivered_unique_events
        ),
        durable_outbox_entries=summary.database_outbox.durable,
        pending_outbox_entries=summary.database_outbox.pending_publication,
        source_queue_visible_messages=source["ApproximateNumberOfMessages"],
        source_queue_in_flight_messages=source["ApproximateNumberOfMessagesNotVisible"],
        source_queue_delayed_messages=source["ApproximateNumberOfMessagesDelayed"],
        dead_letter_queue_messages=dead_letter["ApproximateNumberOfMessages"],
        worker_desired_count=desired,
        worker_running_count=running,
        worker_pending_count=pending,
        api_runtime=api_runtime,
    )


def _write_timeline(
    evidence_root: Path,
    observations: Sequence[FixedControlObservation],
    failures: Sequence[FixedControlObservationFailure],
) -> None:
    (evidence_root / "observations.json").write_text(
        dumps([item.model_dump(mode="json") for item in observations], indent=2) + "\n",
        encoding="utf-8",
    )
    (evidence_root / "observation-failures.json").write_text(
        dumps([item.model_dump(mode="json") for item in failures], indent=2) + "\n",
        encoding="utf-8",
    )


def _failure_kind(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPError):
        return "api-summary"
    if isinstance(error, AwsFixedControlError):
        message = str(error)
        if message.startswith("SQS"):
            return "queue"
        if message.startswith("ECS"):
            return "ecs"
    return "invalid-response"


def _attempt_observation(
    collector: Callable[..., FixedControlObservation],
    *,
    evidence_root: Path,
    observations: list[FixedControlObservation],
    failures: list[FixedControlObservationFailure],
    observed_at: datetime,
    load_started_at: datetime,
    load_ended_at: datetime | None,
) -> FixedControlObservation | None:
    try:
        observation = collector(
            observed_at=observed_at,
            load_started_at=load_started_at,
            load_ended_at=load_ended_at,
        )
    except (AwsFixedControlError, httpx.HTTPError, ValueError) as error:
        failures.append(
            FixedControlObservationFailure(
                observed_at=observed_at,
                seconds_after_load_started=max(
                    0.0,
                    (observed_at - load_started_at).total_seconds(),
                ),
                phase="post-load" if load_ended_at is not None else "during-load",
                kind=_failure_kind(error),
            )
        )
        _write_timeline(evidence_root, observations, failures)
        return None
    observations.append(observation)
    _write_timeline(evidence_root, observations, failures)
    return observation


def _aligned_metric_load_start(
    definition: ElasticityWorkloadDefinition,
    *,
    now: Now,
    sleeper: Sleeper,
) -> datetime:
    """Start near a UTC minute boundary so partial edge buckets publish."""
    candidate = now()
    if candidate.utcoffset() is None:
        raise AwsFixedControlError("fixed-control clock must be timezone-aware")
    seconds_into_period = candidate.astimezone(UTC).timestamp() % (
        definition.metric_period_seconds
    )
    if seconds_into_period > definition.metric_start_alignment_tolerance_seconds:
        sleeper(definition.metric_period_seconds - seconds_into_period)
        candidate = now()
        seconds_into_period = candidate.astimezone(UTC).timestamp() % (
            definition.metric_period_seconds
        )
    if seconds_into_period > definition.metric_start_alignment_tolerance_seconds:
        raise AwsFixedControlError(
            "fixed-control load did not align to the native metric boundary"
        )
    return candidate


def execute_elasticity_workload(
    session: AwsSession,
    *,
    treatment: ElasticityTreatment,
    api_url: str,
    source_queue_url: str,
    dead_letter_queue_url: str,
    cluster_name: str,
    worker_service_name: str,
    evidence_root: Path,
    definition: ElasticityWorkloadDefinition = ELASTICITY_WORKLOAD_DEFINITION,
    runner: ProcessRunner = run_process,
    now: Now = lambda: datetime.now(UTC),
    sleeper: Sleeper = sleep,
    observation_interval_seconds: float = 10,
    post_load_timeout_seconds: float = 1200,
    empty_stability_seconds: float = 180,
    api_client: httpx.Client | None = None,
) -> ElasticityResult:
    """Replay either treatment with identical sampling and drain semantics."""
    manifest = build_elasticity_manifest(
        treatment,
        definition=definition,
    )
    definition_path = evidence_root / "workload-definition.json"
    manifest_path = evidence_root / "input-manifest.json"
    definition_path.write_text(
        definition.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    client_context = (
        nullcontext(api_client)
        if api_client is not None
        else httpx.Client(base_url=api_url, timeout=10)
    )
    observations: list[FixedControlObservation] = []
    failures: list[FixedControlObservationFailure] = []
    with client_context as client:
        registration = client.post(
            "/api/v1/test-runs",
            json=manifest.model_dump(mode="json"),
        )
        registration.raise_for_status()
        command = build_elasticity_k6_command(
            definition,
            manifest_path=manifest_path,
            definition_path=definition_path,
            result_directory=evidence_root,
            api_url_for_container=api_url,
        )
        collector = lambda **times: collect_fixed_control_observation(
            session,
            api_client=client,
            test_run_id=manifest.test_run_id,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            definition=definition,
            runner=runner,
            **times,
        )
        load_started_at = _aligned_metric_load_start(
            definition,
            now=now,
            sleeper=sleeper,
        )
        with (evidence_root / "k6.log").open("w", encoding="utf-8") as log_file:
            process = Popen(
                command,
                stdin=DEVNULL,
                stdout=log_file,
                stderr=STDOUT,
                text=True,
            )
            try:
                while True:
                    observed_at = now()
                    if (observed_at - load_started_at).total_seconds() > (
                        definition.duration_seconds + 120
                    ):
                        raise AwsFixedControlError(
                            "elasticity workload driver timed out"
                        )
                    _attempt_observation(
                        collector,
                        evidence_root=evidence_root,
                        observations=observations,
                        failures=failures,
                        observed_at=observed_at,
                        load_started_at=load_started_at,
                        load_ended_at=None,
                    )
                    try:
                        k6_exit_code = process.wait(
                            timeout=observation_interval_seconds
                        )
                        break
                    except TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except TimeoutExpired:
                        process.kill()
                        process.wait()
        load_ended_at = now()
        completion = client.post(f"/api/v1/test-runs/{manifest.test_run_id}/complete")
        completion.raise_for_status()

        stable_since: datetime | None = None
        drain_stability_confirmed = False
        while (now() - load_ended_at).total_seconds() <= post_load_timeout_seconds:
            observed_at = now()
            observation = _attempt_observation(
                collector,
                evidence_root=evidence_root,
                observations=observations,
                failures=failures,
                observed_at=observed_at,
                load_started_at=load_started_at,
                load_ended_at=load_ended_at,
            )
            if observation is None or not observation.processing_drained:
                stable_since = None
            elif stable_since is None:
                stable_since = observed_at
            elif (
                observed_at - stable_since
            ).total_seconds() >= empty_stability_seconds:
                drain_stability_confirmed = True
                break
            sleeper(observation_interval_seconds)

        reconciliation_response = client.post(
            f"/api/v1/test-runs/{manifest.test_run_id}/reconciliation",
            json=manifest.model_dump(mode="json"),
        )
        reconciliation_response.raise_for_status()
        reconciliation = ReconciliationReport.model_validate(
            reconciliation_response.json()
        )

    try:
        k6_summary = loads(
            (evidence_root / "k6-summary.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, JSONDecodeError) as error:
        raise AwsFixedControlError(
            "k6 did not produce a valid fixed-control summary"
        ) from error
    dropped_iterations = int(
        _metric_value(k6_summary, "dropped_iterations", "count")
        if "dropped_iterations" in k6_summary.get("metrics", {})
        else 0
    )
    result_type = (
        FixedControlResult
        if treatment is ElasticityTreatment.FIXED
        else ElasticityResult
    )
    result = result_type(
        test_run_id=manifest.test_run_id,
        definition=definition,
        load_started_at=load_started_at,
        load_ended_at=load_ended_at,
        k6_exit_code=k6_exit_code,
        dropped_iteration_count=dropped_iterations,
        ingestion_steps=derive_ingestion_steps(definition, k6_summary),
        observations=tuple(observations),
        observation_failures=tuple(failures),
        drain_stability_confirmed=drain_stability_confirmed,
        reconciliation=reconciliation,
    )
    (evidence_root / "result.json").write_text(
        result.model_dump_json(indent=2, round_trip=True) + "\n",
        encoding="utf-8",
    )
    return result


def execute_fixed_control(session: AwsSession, **kwargs: object) -> FixedControlResult:
    """Run the fixed treatment through the shared measurement engine."""
    result = execute_elasticity_workload(
        session, treatment=ElasticityTreatment.FIXED, **kwargs
    )
    assert isinstance(result, FixedControlResult)
    return result


def _valid_fixed_api_url(value: str) -> bool:
    try:
        endpoint = urlsplit(value)
        port = endpoint.port
    except ValueError:
        return False
    return (
        endpoint.scheme == "http"
        and bool(endpoint.hostname)
        and endpoint.username is None
        and endpoint.password is None
        and port is None
        and endpoint.path in ("", "/")
        and not endpoint.query
        and not endpoint.fragment
    )


def _valid_fixed_queue_url(value: str, *, region: str) -> bool:
    try:
        endpoint = urlsplit(value)
        port = endpoint.port
    except ValueError:
        return False
    queue_parts = tuple(part for part in endpoint.path.split("/") if part)
    return (
        endpoint.scheme == "https"
        and endpoint.hostname == f"sqs.{region}.amazonaws.com"
        and endpoint.username is None
        and endpoint.password is None
        and port is None
        and len(queue_parts) == 2
        and not endpoint.query
        and not endpoint.fragment
    )


def _prepare_fixed_control(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    definition: ElasticityWorkloadDefinition,
    runner: ProcessRunner,
    treatment: ElasticityTreatment = ElasticityTreatment.FIXED,
) -> tuple[str, str, str, str, str, Path]:
    """Validate fixed capacity, resolve endpoints, and arm the control."""
    _current, revision = require_clean_approved_revision(session, runner=runner)
    api_url = terraform_output(session, "async_api_url", runner=runner)
    source_queue_url = terraform_output(session, "delivery_queue_url", runner=runner)
    dead_letter_queue_url = terraform_output(
        session,
        "delivery_dead_letter_queue_url",
        runner=runner,
    )
    dimensions = terraform_output(
        session,
        "async_observability_dimensions",
        runner=runner,
        json_output=True,
    )
    capacity = terraform_output(
        session,
        "async_service_capacity",
        runner=runner,
        json_output=True,
    )
    if not all(
        isinstance(value, str)
        for value in (api_url, source_queue_url, dead_letter_queue_url)
    ):
        raise AwsFixedControlError("Terraform returned invalid fixed-control inputs")
    if not _valid_fixed_api_url(api_url):
        raise AwsFixedControlError("Terraform returned an invalid fixed-control API")
    for queue_url in (source_queue_url, dead_letter_queue_url):
        if not _valid_fixed_queue_url(queue_url, region=session.region):
            raise AwsFixedControlError(
                "Terraform returned an invalid fixed-control queue"
            )
    if (
        not isinstance(dimensions, dict)
        or set(dimensions)
        != {
            "api_service_name",
            "cluster_name",
            "dashboard_name",
            "dead_letter_queue_name",
            "delivery_queue_name",
            "load_balancer_dimension",
            "rds_identifier",
            "simulator_service_name",
            "worker_service_name",
        }
        or not all(isinstance(value, str) and value for value in dimensions.values())
        or CLUSTER_NAME_PATTERN.fullmatch(dimensions["cluster_name"]) is None
        or SERVICE_NAME_PATTERN.fullmatch(dimensions["worker_service_name"]) is None
        or not dimensions["worker_service_name"].endswith("-worker")
        or not isinstance(capacity, dict)
        or set(capacity) != set(FIXED_SERVICE_CAPACITY)
    ):
        raise AwsFixedControlError("Terraform returned invalid fixed-control inputs")
    for role, expected in FIXED_SERVICE_CAPACITY.items():
        observed = capacity.get(role)
        if not isinstance(observed, dict):
            raise AwsFixedControlError(
                "Terraform returned invalid fixed-control capacity"
            )
        numeric_capacity = {
            key: observed.get(key)
            for key in ("cpu_units", "desired_count", "memory_mib")
        }
        service_name = observed.get("service_name")
        if (
            set(observed)
            != {"cpu_units", "desired_count", "memory_mib", "service_name"}
            or any(type(value) is not int for value in numeric_capacity.values())
            or numeric_capacity != expected
            or not isinstance(service_name, str)
            or SERVICE_NAME_PATTERN.fullmatch(service_name) is None
            or not service_name.endswith(f"-{role}")
        ):
            raise AwsFixedControlError(
                "Terraform returned invalid fixed-control capacity"
            )
    if (
        capacity["worker"]["desired_count"] != definition.minimum_worker_count
        or capacity["api"]["service_name"] != dimensions["api_service_name"]
        or capacity["simulator"]["service_name"] != dimensions["simulator_service_name"]
        or capacity["worker"]["service_name"] != dimensions["worker_service_name"]
    ):
        raise AwsFixedControlError("Terraform returned inconsistent fixed control")
    evidence_root = session.evidence_dir / "elasticity" / treatment.value
    evidence_root.mkdir(parents=True, exist_ok=False)
    manifest.update(
        {
            (
                "fixed_control"
                if treatment is ElasticityTreatment.FIXED
                else "elastic_treatment"
            ): {
                "definition": definition.model_dump(mode="json"),
                "git_revision": revision,
                "unconditional_teardown_armed": True,
            },
            "status": (
                "fixed_control_armed"
                if treatment is ElasticityTreatment.FIXED
                else "elastic_treatment_armed"
            ),
        }
    )
    write_manifest(session, manifest)
    return (
        api_url,
        source_queue_url,
        dead_letter_queue_url,
        dimensions["cluster_name"],
        dimensions["worker_service_name"],
        evidence_root,
    )


def run_fixed_control_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    definition: ElasticityWorkloadDefinition = ELASTICITY_WORKLOAD_DEFINITION,
    treatment_runner: TreatmentRunner = execute_fixed_control,
    metric_collector: MetricCollector = collect_elasticity_cloudwatch_evidence,
    runner: ProcessRunner = run_process,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
) -> FixedControlSummary:
    """Measure and qualify the fixed control before retaining the stack."""
    manifest = validate_fixed_control_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    try:
        (
            api_url,
            source_queue_url,
            dead_letter_queue_url,
            cluster_name,
            worker_service_name,
            evidence_root,
        ) = _prepare_fixed_control(
            session,
            manifest=manifest,
            definition=definition,
            runner=runner,
        )
        result = treatment_runner(
            session,
            api_url=api_url,
            source_queue_url=source_queue_url,
            dead_letter_queue_url=dead_letter_queue_url,
            cluster_name=cluster_name,
            worker_service_name=worker_service_name,
            evidence_root=evidence_root,
            definition=definition,
            runner=runner,
        )
        cloudwatch = metric_collector(
            session,
            test_run_id=result.test_run_id,
            window_started_at=result.load_started_at,
            window_ended_at=result.load_ended_at,
            runner=runner,
        )
        qualification = evaluate_fixed_control_qualification(result, cloudwatch)
        summary = FixedControlSummary(
            measurement=result,
            cloudwatch=cloudwatch,
            qualification=qualification,
        )
        (evidence_root / "cloudwatch.json").write_text(
            cloudwatch.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (evidence_root / "summary.json").write_text(
            summary.model_dump_json(indent=2, round_trip=True) + "\n",
            encoding="utf-8",
        )
        manifest = load_manifest(session)
        manifest["fixed_control"] = {
            **manifest["fixed_control"],
            "cloudwatch": "elasticity/fixed/cloudwatch.json",
            "measurement_complete": result.measurement_complete,
            "qualified": qualification.qualified,
            "result": "elasticity/fixed/result.json",
            "summary": "elasticity/fixed/summary.json",
            "worker_pressure_observed": result.worker_pressure_observed,
        }
        manifest["status"] = (
            "fixed_control_qualified"
            if qualification.qualified
            else "fixed_control_rejected"
        )
        write_manifest(session, manifest)
        if not qualification.qualified:
            raise AwsFixedControlQualificationError(
                "fixed-control candidate was rejected: "
                + ", ".join(qualification.rejection_reasons)
            )
        return summary
    except BaseException as workflow_error:
        cleanup_errors = []
        try:
            destroyer(session)
        except BaseException as error:  # noqa: BLE001 - still verify natively
            cleanup_errors.append(error)
        try:
            teardown_verifier(session)
        except BaseException as error:  # noqa: BLE001 - report every cleanup failure
            cleanup_errors.append(error)
        if cleanup_errors:
            raise AwsFixedControlCleanupError(
                "Stage 9.6 fixed control failed and cleanup did not complete",
                workflow_error=workflow_error,
                cleanup_errors=cleanup_errors,
            ) from workflow_error
        raise


def build_parser() -> ArgumentParser:
    """Build the explicitly armed fixed-control command."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    parser.add_argument("--approved-session-id", required=True)
    parser.add_argument("--approved-cost-ceiling-usd", required=True)
    parser.add_argument(
        "--approved-unconditional-teardown-session-id",
        required=True,
    )
    return parser


def run_from_arguments(arguments: Namespace) -> None:
    """Build the session and execute the fixed control."""
    run_fixed_control_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt("received SIGTERM during Stage 9.6 fixed control")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed control and translate expected failures concisely."""
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsFixedControlCleanupError,
        AwsFixedControlError,
        AwsAsyncDeploymentError,
        AwsElasticityCloudWatchError,
        AwsSessionError,
        KeyboardInterrupt,
        httpx.HTTPError,
    ) as error:
        raise SystemExit(f"AWS fixed control failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
