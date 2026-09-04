"""Run the tiny Stage 9.5 async integration workloads and always tear down."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from json import JSONDecodeError, dumps, loads
from math import ceil, isfinite
from pathlib import Path
from signal import SIGTERM, getsignal, signal
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_async_deployment import (
    ProcessRunner,
    aws_prefix,
    invoke,
    require_clean_approved_revision,
    run_process,
    terraform_output,
)
from trackrelay.aws_observability_inputs import valid_async_observability_dimensions
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
from trackrelay.experiments.async_guardrails import (
    ASYNC_PROCESSING_GUARDRAILS,
    AsyncProcessingEvidence,
    AsyncProcessingGuardrailDefinition,
    AsyncProcessingGuardrailEvaluation,
    AsyncProcessingObservation,
    evaluate_async_processing_guardrails,
)
from trackrelay.experiments.generator import (
    GeneratorConfiguration,
    InputManifest,
    generate_input_manifest,
)
from trackrelay.experiments.reconciliation import ReconciliationReport

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
Now = Callable[[], datetime]
Sleeper = Callable[[float], None]
SessionAction = Callable[..., object]
PointRunner = Callable[..., "AsyncIntegrationPointResult"]
MetricCollector = Callable[..., "AsyncCloudWatchEvidence"]
FROZEN_EVENT_COUNTS = (1, 10, 100)


class AwsAsyncIntegrationError(RuntimeError):
    """A safe, actionable Stage 9.5 integration failure."""


class AwsAsyncIntegrationCleanupError(RuntimeError):
    """Report teardown failures without hiding an integration failure."""

    def __init__(
        self,
        message: str,
        *,
        workflow_error: BaseException | None,
        cleanup_errors: Sequence[BaseException],
    ) -> None:
        super().__init__(message)
        self.workflow_error = workflow_error
        self.cleanup_errors = tuple(cleanup_errors)


class AsyncIntegrationDefinition(BaseModel):
    """Frozen tiny-workload contract for cloud session 3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-async-integration-v1"] = "aws-async-integration-v1"
    event_counts: tuple[PositiveInteger, ...] = FROZEN_EVENT_COUNTS
    random_seed: int = 20260901
    partner_id: str = "async-integration-alpha"
    observation_interval_seconds: Annotated[float, Field(gt=0, le=20)] = 10
    observation_timeout_seconds: Annotated[float, Field(gt=0)] = 330
    request_timeout_seconds: Annotated[float, Field(gt=0)] = 10
    ingestion_p95_limit_ms: PositiveInteger = 500
    ingestion_error_limit_percent: Annotated[float, Field(gt=0)] = 1
    processing_guardrails: AsyncProcessingGuardrailDefinition = (
        ASYNC_PROCESSING_GUARDRAILS
    )

    @model_validator(mode="after")
    def require_frozen_workloads_and_complete_drain_window(
        self,
    ) -> "AsyncIntegrationDefinition":
        if self.event_counts != FROZEN_EVENT_COUNTS:
            raise ValueError(
                "integration workloads must be exactly 1, 10, and 100 events"
            )
        minimum_timeout = (
            self.processing_guardrails.drain_deadline_seconds
            + self.processing_guardrails.empty_stability_seconds
            + self.processing_guardrails.maximum_observation_gap_seconds
        )
        if self.observation_timeout_seconds < minimum_timeout:
            raise ValueError("observation timeout cannot cover the drain contract")
        return self


ASYNC_INTEGRATION_DEFINITION = AsyncIntegrationDefinition()


class AsyncRequestObservation(BaseModel):
    """One externally observed ingestion response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_number: PositiveInteger
    status_code: int
    latency_ms: Annotated[float, Field(ge=0)]


class AsyncRunDatabaseEvents(BaseModel):
    """Database event counts returned by the restricted evidence API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persisted: NonNegativeInteger
    processed: NonNegativeInteger
    failed: NonNegativeInteger
    pending: NonNegativeInteger


class AsyncRunDeliveryAttempts(BaseModel):
    """Delivery-attempt counts returned by the restricted evidence API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: NonNegativeInteger
    delivered: NonNegativeInteger
    delivered_unique_events: NonNegativeInteger
    http_error: NonNegativeInteger
    transport_error: NonNegativeInteger


class AsyncRunOutbox(BaseModel):
    """Durable outbox counts returned by the restricted evidence API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    durable: NonNegativeInteger
    pending_publication: NonNegativeInteger


class AsyncRunSummary(BaseModel):
    """Subset of the API summary required for processing observations."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    test_run_id: UUID
    declared_event_count: NonNegativeInteger
    completed_at: datetime | None
    database_events: AsyncRunDatabaseEvents
    database_delivery_attempts: AsyncRunDeliveryAttempts
    database_outbox: AsyncRunOutbox


class AsyncIntegrationPointResult(BaseModel):
    """Complete result for one tiny integration workload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_count: PositiveInteger
    test_run_id: UUID
    load_started_at: AwareDatetime
    load_ended_at: AwareDatetime
    request_observations: tuple[AsyncRequestObservation, ...]
    ingestion_p95_ms: Annotated[float, Field(ge=0)]
    ingestion_error_percent: Annotated[float, Field(ge=0, le=100)]
    processing: AsyncProcessingGuardrailEvaluation
    guardrails_passed: bool

    @model_validator(mode="after")
    def require_consistent_point(self) -> "AsyncIntegrationPointResult":
        if self.load_ended_at < self.load_started_at:
            raise ValueError("integration load window is reversed")
        if self.event_count != len(self.request_observations):
            raise ValueError("request observations do not match the event count")
        expected_pass = (
            self.ingestion_p95_ms < 500
            and self.ingestion_error_percent < 1
            and self.processing.guardrails_passed
        )
        if self.guardrails_passed is not expected_pass:
            raise ValueError("point guardrail summary disagrees with evidence")
        return self


class AsyncCloudWatchMetricEvidence(BaseModel):
    """One native CloudWatch series proving the session telemetry path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    timestamps: tuple[AwareDatetime, ...]
    values: tuple[float, ...]

    @model_validator(mode="after")
    def require_published_finite_samples(self) -> "AsyncCloudWatchMetricEvidence":
        if not self.timestamps or len(self.timestamps) != len(self.values):
            raise ValueError("CloudWatch evidence requires aligned published samples")
        if tuple(sorted(self.timestamps)) != self.timestamps:
            raise ValueError("CloudWatch evidence timestamps must be ordered")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("CloudWatch evidence values must be finite")
        return self


class AsyncCloudWatchEvidence(BaseModel):
    """Dashboard and published native metrics for the integration window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    collected_at: AwareDatetime
    window_started_at: AwareDatetime
    window_ended_at: AwareDatetime
    dashboard_widget_count: Literal[7] = 7
    series: tuple[AsyncCloudWatchMetricEvidence, ...]

    @model_validator(mode="after")
    def require_complete_metric_path(self) -> "AsyncCloudWatchEvidence":
        expected = {
            "alb_requests",
            "api_cpu",
            "queue_sent",
            "rds_cpu",
            "simulator_cpu",
            "worker_tasks",
        }
        observed = {item.query_id for item in self.series}
        if observed != expected or len(observed) != len(self.series):
            raise ValueError("CloudWatch integration evidence is incomplete")
        if self.window_ended_at <= self.window_started_at:
            raise ValueError("CloudWatch integration window must be positive")
        return self


class AsyncIntegrationSummary(BaseModel):
    """Compact index for the complete cloud-session-3 evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    completed_at: AwareDatetime
    definition: AsyncIntegrationDefinition
    points: tuple[AsyncIntegrationPointResult, ...]
    cloudwatch: AsyncCloudWatchEvidence
    all_guardrails_passed: bool

    @model_validator(mode="after")
    def require_all_frozen_points(self) -> "AsyncIntegrationSummary":
        if (
            tuple(point.event_count for point in self.points)
            != self.definition.event_counts
        ):
            raise ValueError(
                "integration summary does not contain the frozen workloads"
            )
        expected_pass = all(point.guardrails_passed for point in self.points)
        if self.all_guardrails_passed is not expected_pass:
            raise ValueError("integration summary disagrees with point guardrails")
        return self


def build_exact_event_manifest(
    event_count: int,
    *,
    definition: AsyncIntegrationDefinition = ASYNC_INTEGRATION_DEFINITION,
    test_run_id: UUID | None = None,
) -> InputManifest:
    """Build exactly N deterministic events, including partial shipment history."""
    statuses_per_shipment = 5
    configuration = GeneratorConfiguration(
        partner_id=definition.partner_id,
        shipment_count=ceil(event_count / statuses_per_shipment),
    )
    generated = generate_input_manifest(
        seed=definition.random_seed + event_count,
        configuration=configuration,
        test_run_id=test_run_id or uuid4(),
    )
    expected_events = generated.expected_events[:event_count]
    final_shipments = {
        tracking_number: next(
            event.expected_status
            for event in reversed(expected_events)
            if event.tracking_number == tracking_number
        )
        for tracking_number in {event.tracking_number for event in expected_events}
    }
    return InputManifest(
        test_run_id=generated.test_run_id,
        scenario_name=generated.scenario_name,
        seed=generated.seed,
        configuration=generated.configuration,
        events_generated=event_count,
        expected_unique_events=event_count,
        expected_events=expected_events,
        expected_final_shipments=final_shipments,
    )


def _nearest_rank_p95(values: Sequence[float]) -> float:
    if not values:
        raise AwsAsyncIntegrationError("cannot calculate p95 without requests")
    ordered = sorted(values)
    return ordered[ceil(0.95 * len(ordered)) - 1]


def _queue_attributes(
    session: AwsSession,
    queue_url: str,
    *,
    runner: ProcessRunner,
) -> dict[str, int]:
    result = invoke(
        runner,
        (
            *aws_prefix(session),
            "sqs",
            "get-queue-attributes",
            "--queue-url",
            queue_url,
            "--attribute-names",
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
            "--query",
            "Attributes",
            "--output",
            "json",
        ),
        action="SQS integration observation",
    )
    try:
        parsed = loads(result.stdout)
        values = {
            name: int(parsed[name])
            for name in (
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            )
        }
    except (JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AwsAsyncIntegrationError(
            "SQS returned invalid integration attributes"
        ) from error
    if any(value < 0 for value in values.values()):
        raise AwsAsyncIntegrationError("SQS returned negative integration attributes")
    return values


def _processing_observation(
    *,
    summary: AsyncRunSummary,
    source_attributes: dict[str, int],
    dead_letter_attributes: dict[str, int],
    load_ended_at: datetime,
    observed_at: datetime,
) -> AsyncProcessingObservation:
    return AsyncProcessingObservation(
        observed_at=observed_at,
        seconds_after_load_ended=(observed_at - load_ended_at).total_seconds(),
        accepted_unique_events=summary.database_events.persisted,
        durable_outbox_entries=summary.database_outbox.durable,
        completed_delivery_events=(
            summary.database_delivery_attempts.delivered_unique_events
        ),
        pending_outbox_entries=summary.database_outbox.pending_publication,
        source_queue_visible_messages=source_attributes["ApproximateNumberOfMessages"],
        source_queue_in_flight_messages=source_attributes[
            "ApproximateNumberOfMessagesNotVisible"
        ],
        source_queue_delayed_messages=source_attributes[
            "ApproximateNumberOfMessagesDelayed"
        ],
        dead_letter_queue_messages=dead_letter_attributes[
            "ApproximateNumberOfMessages"
        ],
    )


def run_async_integration_point(
    session: AwsSession,
    *,
    event_count: int,
    api_url: str,
    source_queue_url: str,
    dead_letter_queue_url: str,
    definition: AsyncIntegrationDefinition = ASYNC_INTEGRATION_DEFINITION,
    runner: ProcessRunner = run_process,
    now: Now = lambda: datetime.now(UTC),
    sleeper: Sleeper = sleep,
    timer: Callable[[], float] = monotonic,
    client: httpx.Client | None = None,
) -> AsyncIntegrationPointResult:
    """Exercise one exact workload and retain authoritative drain evidence."""
    manifest = build_exact_event_manifest(event_count, definition=definition)
    point_directory = (
        session.evidence_dir / "async-integration" / f"events-{event_count}"
    )
    point_directory.mkdir(parents=True, exist_ok=False)
    (point_directory / "input-manifest.json").write_text(
        f"{manifest.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    client_context = (
        nullcontext(client)
        if client is not None
        else httpx.Client(
            base_url=api_url,
            timeout=definition.request_timeout_seconds,
        )
    )
    with client_context as api_client:
        registration = api_client.post(
            "/api/v1/test-runs",
            json=manifest.model_dump(mode="json"),
        )
        registration.raise_for_status()
        load_started_at = now()
        request_observations = []
        for event in manifest.expected_events:
            started = timer()
            response = api_client.post(
                f"/api/v1/partners/{event.partner_id}/events",
                headers={"X-Test-Run-ID": str(manifest.test_run_id)},
                json=event.payload,
            )
            request_observations.append(
                AsyncRequestObservation(
                    sequence_number=event.sequence_number,
                    status_code=response.status_code,
                    latency_ms=(timer() - started) * 1000,
                )
            )
            (point_directory / "request-observations.json").write_text(
                dumps(
                    [item.model_dump(mode="json") for item in request_observations],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        load_ended_at = now()
        completion = api_client.post(
            f"/api/v1/test-runs/{manifest.test_run_id}/complete"
        )
        completion.raise_for_status()

        observations: list[AsyncProcessingObservation] = []
        stable_since: float | None = None
        previous_elapsed: float | None = None
        while True:
            observed_at = now()
            elapsed = (observed_at - load_ended_at).total_seconds()
            summary_response = api_client.get(
                f"/api/v1/test-runs/{manifest.test_run_id}/summary"
            )
            summary_response.raise_for_status()
            summary = AsyncRunSummary.model_validate(summary_response.json())
            source_attributes = _queue_attributes(
                session,
                source_queue_url,
                runner=runner,
            )
            dead_letter_attributes = _queue_attributes(
                session,
                dead_letter_queue_url,
                runner=runner,
            )
            observation = _processing_observation(
                summary=summary,
                source_attributes=source_attributes,
                dead_letter_attributes=dead_letter_attributes,
                load_ended_at=load_ended_at,
                observed_at=observed_at,
            )
            observations.append(observation)
            (point_directory / "processing-observations.json").write_text(
                dumps(
                    [item.model_dump(mode="json") for item in observations],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            gap_too_large = (
                previous_elapsed is not None
                and elapsed - previous_elapsed
                > definition.processing_guardrails.maximum_observation_gap_seconds
            )
            previous_elapsed = elapsed
            if gap_too_large or not observation.processing_drained:
                stable_since = None
            elif stable_since is None:
                stable_since = elapsed
            if (
                stable_since is not None
                and elapsed - stable_since
                >= definition.processing_guardrails.empty_stability_seconds
            ):
                break
            if elapsed >= definition.observation_timeout_seconds:
                raise AwsAsyncIntegrationError(
                    f"{event_count}-event workload did not confirm a stable drain"
                )
            sleeper(definition.observation_interval_seconds)

        reconciliation_response = api_client.post(
            f"/api/v1/test-runs/{manifest.test_run_id}/reconciliation",
            json=manifest.model_dump(mode="json"),
        )
        reconciliation_response.raise_for_status()
        reconciliation = ReconciliationReport.model_validate(
            reconciliation_response.json()
        )

    processing_evidence = AsyncProcessingEvidence(
        test_run_id=manifest.test_run_id,
        reconciliation=reconciliation,
        observations=tuple(observations),
    )
    processing = evaluate_async_processing_guardrails(
        processing_evidence,
        definition=definition.processing_guardrails,
    )
    latencies = tuple(item.latency_ms for item in request_observations)
    error_count = sum(
        not 200 <= item.status_code < 300 for item in request_observations
    )
    ingestion_p95_ms = _nearest_rank_p95(latencies)
    ingestion_error_percent = 100 * error_count / event_count
    guardrails_passed = (
        ingestion_p95_ms < definition.ingestion_p95_limit_ms
        and ingestion_error_percent < definition.ingestion_error_limit_percent
        and processing.guardrails_passed
    )
    result = AsyncIntegrationPointResult(
        event_count=event_count,
        test_run_id=manifest.test_run_id,
        load_started_at=load_started_at,
        load_ended_at=load_ended_at,
        request_observations=tuple(request_observations),
        ingestion_p95_ms=ingestion_p95_ms,
        ingestion_error_percent=ingestion_error_percent,
        processing=processing,
        guardrails_passed=guardrails_passed,
    )
    (point_directory / "result.json").write_text(
        f"{result.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    if not result.guardrails_passed:
        raise AwsAsyncIntegrationError(
            f"{event_count}-event integration guardrails failed"
        )
    return result


def _async_metric_queries(dimensions: dict[str, str]) -> list[dict[str, object]]:
    definitions = (
        (
            "alb_requests",
            "AWS/ApplicationELB",
            "RequestCount",
            "Sum",
            [("LoadBalancer", "load_balancer_dimension")],
        ),
        (
            "api_cpu",
            "AWS/ECS",
            "CPUUtilization",
            "Maximum",
            [("ClusterName", "cluster_name"), ("ServiceName", "api_service_name")],
        ),
        (
            "simulator_cpu",
            "AWS/ECS",
            "CPUUtilization",
            "Maximum",
            [
                ("ClusterName", "cluster_name"),
                ("ServiceName", "simulator_service_name"),
            ],
        ),
        (
            "worker_tasks",
            "ECS/ContainerInsights",
            "RunningTaskCount",
            "Average",
            [
                ("ClusterName", "cluster_name"),
                ("ServiceName", "worker_service_name"),
            ],
        ),
        (
            "queue_sent",
            "AWS/SQS",
            "NumberOfMessagesSent",
            "Sum",
            [("QueueName", "delivery_queue_name")],
        ),
        (
            "rds_cpu",
            "AWS/RDS",
            "CPUUtilization",
            "Maximum",
            [("DBInstanceIdentifier", "rds_identifier")],
        ),
    )
    return [
        {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": name, "Value": dimensions[key]}
                        for name, key in dimension_keys
                    ],
                },
                "Period": 60,
                "Stat": statistic,
            },
            "ReturnData": True,
        }
        for query_id, namespace, metric_name, statistic, dimension_keys in definitions
    ]


def collect_async_cloudwatch_evidence(
    session: AwsSession,
    *,
    window_started_at: datetime,
    window_ended_at: datetime,
    runner: ProcessRunner = run_process,
    now: Now = lambda: datetime.now(UTC),
    sleeper: Sleeper = sleep,
    maximum_attempts: int = 4,
    retry_interval_seconds: float = 15,
) -> AsyncCloudWatchEvidence:
    """Require the dashboard and representative native metrics to be published."""
    raw_dimensions = terraform_output(
        session,
        "async_observability_dimensions",
        runner=runner,
        json_output=True,
    )
    if not valid_async_observability_dimensions(raw_dimensions):
        raise AwsAsyncIntegrationError(
            "Terraform returned invalid CloudWatch dimensions"
        )
    dimensions: dict[str, str] = raw_dimensions
    dashboard_result = invoke(
        runner,
        (
            *aws_prefix(session),
            "cloudwatch",
            "get-dashboard",
            "--dashboard-name",
            dimensions["dashboard_name"],
            "--output",
            "json",
        ),
        action="CloudWatch integration dashboard inspection",
    )
    try:
        dashboard_document = loads(dashboard_result.stdout)
        dashboard_body = loads(dashboard_document["DashboardBody"])
        widgets = dashboard_body["widgets"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsAsyncIntegrationError(
            "CloudWatch returned invalid dashboard evidence"
        ) from error
    if (
        dashboard_document.get("DashboardName") != dimensions["dashboard_name"]
        or not isinstance(widgets, list)
        or len(widgets) != 7
        or any(widget.get("type") != "metric" for widget in widgets)
    ):
        raise AwsAsyncIntegrationError(
            "CloudWatch dashboard differs from the approved contract"
        )

    query_document = _async_metric_queries(dimensions)
    last_problem = "no metric query was attempted"
    for attempt in range(1, maximum_attempts + 1):
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
        ) as query_file:
            query_file.write(dumps(query_document))
            query_file.flush()
            metric_result = invoke(
                runner,
                (
                    *aws_prefix(session),
                    "cloudwatch",
                    "get-metric-data",
                    "--metric-data-queries",
                    f"file://{query_file.name}",
                    "--start-time",
                    window_started_at.isoformat(),
                    "--end-time",
                    window_ended_at.isoformat(),
                    "--scan-by",
                    "TimestampAscending",
                    "--max-datapoints",
                    "1008",
                    "--output",
                    "json",
                ),
                action="CloudWatch integration metric collection",
            )
        try:
            results = loads(metric_result.stdout)["MetricDataResults"]
            by_id = {item["Id"]: item for item in results}
        except (JSONDecodeError, KeyError, TypeError) as error:
            raise AwsAsyncIntegrationError(
                "CloudWatch returned invalid integration metrics"
            ) from error
        expected_ids = {query["Id"] for query in query_document}
        incomplete = sorted(
            query_id
            for query_id in expected_ids
            if query_id not in by_id
            or by_id[query_id].get("StatusCode") != "Complete"
            or not by_id[query_id].get("Timestamps")
            or len(by_id[query_id].get("Timestamps", []))
            != len(by_id[query_id].get("Values", []))
        )
        if not incomplete and set(by_id) == expected_ids:
            series = tuple(
                AsyncCloudWatchMetricEvidence(
                    query_id=query_id,
                    timestamps=tuple(by_id[query_id]["Timestamps"]),
                    values=tuple(by_id[query_id]["Values"]),
                )
                for query_id in sorted(expected_ids)
            )
            return AsyncCloudWatchEvidence(
                collected_at=now(),
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                series=series,
            )
        last_problem = f"unpublished series: {incomplete}"
        if attempt < maximum_attempts:
            sleeper(retry_interval_seconds)
    raise AwsAsyncIntegrationError(
        "CloudWatch integration evidence remained incomplete; " + last_problem
    )


def validate_async_integration_approval(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
) -> dict[str, object]:
    """Require the exact approved, deployed session before integration."""
    if not session.session_id.startswith("cloud-session-3-"):
        raise AwsSessionError("Stage 9.5 integration must use cloud session 3")
    if session.deployment_mode != "async":
        raise AwsSessionError("Stage 9.5 integration requires async mode")
    if approved_session_id != session.session_id:
        raise AwsSessionError("approved session ID does not match")
    if approved_unconditional_teardown_session_id != session.session_id:
        raise AwsSessionError("approved teardown session ID does not match")
    manifest = load_manifest(session)
    if manifest.get("status") != "async_deployed":
        raise AwsSessionError("integration requires the converged async deployment")
    recorded_ceiling = manifest.get("approved_cost_ceiling_usd")
    if not isinstance(recorded_ceiling, str) or parse_positive_money(
        approved_cost_ceiling_usd,
        field_name="approved cost ceiling",
    ) != parse_positive_money(recorded_ceiling, field_name="recorded cost ceiling"):
        raise AwsSessionError("approved cost ceiling differs from Terraform apply")
    return manifest


def _prepare_async_integration(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    definition: AsyncIntegrationDefinition,
    runner: ProcessRunner,
) -> tuple[str, str, str, Path]:
    """Resolve private endpoints and journal the armed integration."""
    _manifest, revision = require_clean_approved_revision(
        session,
        runner=runner,
    )
    api_url = terraform_output(session, "async_api_url", runner=runner)
    source_queue_url = terraform_output(
        session,
        "delivery_queue_url",
        runner=runner,
    )
    dead_letter_queue_url = terraform_output(
        session,
        "delivery_dead_letter_queue_url",
        runner=runner,
    )
    if not all(
        isinstance(value, str)
        for value in (api_url, source_queue_url, dead_letter_queue_url)
    ):
        raise AwsAsyncIntegrationError(
            "Terraform returned invalid integration endpoints"
        )
    api_endpoint = urlsplit(api_url)
    queue_endpoints = tuple(
        urlsplit(value) for value in (source_queue_url, dead_letter_queue_url)
    )
    if (
        api_endpoint.scheme != "http"
        or not api_endpoint.hostname
        or api_endpoint.path not in ("", "/")
        or api_endpoint.query
        or api_endpoint.fragment
        or any(
            endpoint.scheme != "https"
            or endpoint.hostname != f"sqs.{session.region}.amazonaws.com"
            or not endpoint.path.strip("/")
            or endpoint.query
            or endpoint.fragment
            for endpoint in queue_endpoints
        )
    ):
        raise AwsAsyncIntegrationError(
            "Terraform returned invalid integration endpoints"
        )

    evidence_root = session.evidence_dir / "async-integration"
    evidence_root.mkdir(parents=True, exist_ok=False)
    manifest.update(
        {
            "async_integration": {
                "definition": definition.model_dump(mode="json"),
                "git_revision": revision,
                "unconditional_teardown_armed": True,
            },
            "status": "async_integration_armed",
        }
    )
    write_manifest(session, manifest)
    return api_url, source_queue_url, dead_letter_queue_url, evidence_root


def run_async_integration_session(
    session: AwsSession,
    *,
    approved_session_id: str,
    approved_cost_ceiling_usd: str,
    approved_unconditional_teardown_session_id: str,
    definition: AsyncIntegrationDefinition = ASYNC_INTEGRATION_DEFINITION,
    point_runner: PointRunner = run_async_integration_point,
    metric_collector: MetricCollector = collect_async_cloudwatch_evidence,
    runner: ProcessRunner = run_process,
    destroyer: SessionAction = destroy_session,
    teardown_verifier: SessionAction = verify_destroyed,
    now: Now = lambda: datetime.now(UTC),
) -> AsyncIntegrationSummary:
    """Run all tiny points and unconditionally destroy and verify the stack."""
    manifest = validate_async_integration_approval(
        session,
        approved_session_id=approved_session_id,
        approved_cost_ceiling_usd=approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            approved_unconditional_teardown_session_id
        ),
    )
    summary: AsyncIntegrationSummary | None = None
    workflow_error: BaseException | None = None
    try:
        (
            api_url,
            source_queue_url,
            dead_letter_queue_url,
            evidence_root,
        ) = _prepare_async_integration(
            session,
            manifest=manifest,
            definition=definition,
            runner=runner,
        )
        points = tuple(
            point_runner(
                session,
                event_count=event_count,
                api_url=api_url,
                source_queue_url=source_queue_url,
                dead_letter_queue_url=dead_letter_queue_url,
                definition=definition,
                runner=runner,
            )
            for event_count in definition.event_counts
        )
        cloudwatch = metric_collector(
            session,
            window_started_at=points[0].load_started_at,
            window_ended_at=(
                points[-1].processing.evidence.observations[-1].observed_at
            ),
            runner=runner,
        )
        summary = AsyncIntegrationSummary(
            completed_at=now(),
            definition=definition,
            points=points,
            cloudwatch=cloudwatch,
            all_guardrails_passed=all(point.guardrails_passed for point in points),
        )
        (evidence_root / "summary.json").write_text(
            f"{summary.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
        manifest = load_manifest(session)
        manifest["async_integration"] = {
            **manifest["async_integration"],
            "completed_at": summary.completed_at.isoformat(),
            "result": "async-integration/summary.json",
        }
        manifest["status"] = "async_integration_passed"
        write_manifest(session, manifest)
    except BaseException as error:  # noqa: BLE001 - teardown follows interrupts
        workflow_error = error

    cleanup_errors: list[BaseException] = []
    try:
        destroyer(session)
    except BaseException as error:  # noqa: BLE001 - still verify natively
        cleanup_errors.append(error)
    try:
        teardown_verifier(session)
    except BaseException as error:  # noqa: BLE001 - report every cleanup failure
        cleanup_errors.append(error)
    if cleanup_errors:
        message = "Stage 9.5 integration cleanup did not complete: " + "; ".join(
            f"{type(error).__name__}: {error}" for error in cleanup_errors
        )
        if workflow_error is not None:
            message = (
                "Stage 9.5 integration failed with "
                f"{type(workflow_error).__name__}: {workflow_error}; {message}"
            )
        raise AwsAsyncIntegrationCleanupError(
            message,
            workflow_error=workflow_error,
            cleanup_errors=cleanup_errors,
        ) from (workflow_error or cleanup_errors[0])
    if workflow_error is not None:
        raise workflow_error
    if summary is None:
        raise AssertionError("Stage 9.5 integration completed without a summary")
    return summary


def build_parser() -> ArgumentParser:
    """Build the explicitly armed integration-and-teardown command."""
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
    """Build the session object and execute the armed integration workflow."""
    run_async_integration_session(
        session_from_arguments(arguments),
        approved_session_id=arguments.approved_session_id,
        approved_cost_ceiling_usd=arguments.approved_cost_ceiling_usd,
        approved_unconditional_teardown_session_id=(
            arguments.approved_unconditional_teardown_session_id
        ),
    )


def _terminate_after_cleanup(_signum: int, _frame: object) -> NoReturn:
    """Translate SIGTERM so unconditional teardown remains active."""
    raise KeyboardInterrupt("received SIGTERM during Stage 9.5 integration")


def main(argv: Sequence[str] | None = None) -> int:
    """Run integration and translate expected failures concisely."""
    previous_sigterm_handler = getsignal(SIGTERM)
    signal(SIGTERM, _terminate_after_cleanup)
    try:
        run_from_arguments(build_parser().parse_args(argv))
    except (
        AwsAsyncIntegrationCleanupError,
        AwsAsyncIntegrationError,
        AwsSessionError,
        httpx.HTTPError,
        KeyboardInterrupt,
    ) as error:
        raise SystemExit(f"AWS async integration failed: {error}") from error
    finally:
        signal(SIGTERM, previous_sigterm_handler)
    print("Stage 9.5 integration passed and teardown was verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
