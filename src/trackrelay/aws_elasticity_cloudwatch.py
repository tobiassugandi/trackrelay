"""Collect complete native CloudWatch evidence for an elasticity treatment."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError, dumps, loads
from math import ceil, floor, isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from trackrelay.aws_async_deployment import (
    ProcessRunner,
    aws_prefix,
    invoke,
    run_process,
    terraform_output,
)
from trackrelay.aws_observability_inputs import valid_async_observability_dimensions
from trackrelay.aws_scaling_diagnostics import collect_scaling_diagnostics
from trackrelay.aws_session import AwsSession
from trackrelay.operator_status import operator_status

NonNegativeFloat = Annotated[float, Field(ge=0)]
Now = Callable[[], datetime]
Sleeper = Callable[[float], None]
Statistic = Literal["Average", "Maximum", "Minimum", "Sum", "p95"]
PERIOD_SECONDS = 60


class AwsElasticityCloudWatchError(RuntimeError):
    """A safe failure while collecting Stage 9.6/9.7 native evidence."""


class IncompleteElasticityCloudWatchError(AwsElasticityCloudWatchError):
    """A retryable delay in publication of a complete native window."""


class ElasticityCloudWatchDatapoint(BaseModel):
    """One finite value at the start of its native UTC minute bucket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_started_at: AwareDatetime
    value: float

    @model_validator(mode="after")
    def require_finite_value(self) -> "ElasticityCloudWatchDatapoint":
        if not isfinite(self.value):
            raise ValueError("CloudWatch datapoint must be finite")
        return self


class ElasticityCloudWatchMetricSeries(BaseModel):
    """One complete native metric series aligned to the treatment window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    namespace: str
    metric_name: str
    statistic: Statistic
    unit: str
    datapoints: tuple[ElasticityCloudWatchDatapoint, ...]

    @model_validator(mode="after")
    def require_ordered_unique_datapoints(
        self,
    ) -> "ElasticityCloudWatchMetricSeries":
        timestamps = tuple(point.interval_started_at for point in self.datapoints)
        if not timestamps or timestamps != tuple(sorted(set(timestamps))):
            raise ValueError("CloudWatch series must be nonempty, ordered, and unique")
        return self


class ElasticityCloudWatchEvidence(BaseModel):
    """Complete 60-second AWS evidence for one identified treatment run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    test_run_id: UUID
    window_started_at: AwareDatetime
    window_ended_at: AwareDatetime
    collected_at: AwareDatetime
    period_seconds: Literal[60] = 60
    dashboard_widget_count: Literal[7] = 7
    series: tuple[ElasticityCloudWatchMetricSeries, ...]

    @model_validator(mode="after")
    def require_complete_native_window(self) -> "ElasticityCloudWatchEvidence":
        if self.window_ended_at <= self.window_started_at:
            raise ValueError("CloudWatch treatment window must be positive")
        expected_ids = {
            definition.query_id
            for definition in metric_definitions(schema_version=self.schema_version)
        }
        observed_ids = {series.query_id for series in self.series}
        if observed_ids != expected_ids or len(observed_ids) != len(self.series):
            raise ValueError("CloudWatch treatment evidence is incomplete")
        expected_timestamps = expected_bucket_starts(
            self.window_started_at,
            self.window_ended_at,
        )
        if any(
            tuple(point.interval_started_at for point in series.datapoints)
            != expected_timestamps
            for series in self.series
        ):
            raise ValueError("CloudWatch series do not cover every native bucket")
        return self

    def series_by_id(self) -> dict[str, ElasticityCloudWatchMetricSeries]:
        """Index the already validated unique series."""
        return {series.query_id: series for series in self.series}


class MetricDefinition(NamedTuple):
    """One stable native metric and its Terraform-provided dimensions."""

    query_id: str
    namespace: str
    metric_name: str
    statistic: Statistic
    unit: str
    dimensions: tuple[tuple[str, str], ...]
    fill_missing_with_zero: bool = False


def metric_definitions(
    *, schema_version: Literal[1, 2] = 2
) -> tuple[MetricDefinition, ...]:
    """Return the shared fixed/elastic native evidence contract."""
    alb = (("LoadBalancer", "load_balancer_dimension"),)
    api = (
        ("ClusterName", "cluster_name"),
        ("ServiceName", "api_service_name"),
    )
    worker = (
        ("ClusterName", "cluster_name"),
        ("ServiceName", "worker_service_name"),
    )
    simulator = (
        ("ClusterName", "cluster_name"),
        ("ServiceName", "simulator_service_name"),
    )
    source_queue = (("QueueName", "delivery_queue_name"),)
    dead_letter_queue = (("QueueName", "dead_letter_queue_name"),)
    rds = (("DBInstanceIdentifier", "rds_identifier"),)
    definitions = (
        MetricDefinition(
            "alb_requests", "AWS/ApplicationELB", "RequestCount", "Sum", "Count", alb
        ),
        MetricDefinition(
            "alb_p95_latency",
            "AWS/ApplicationELB",
            "TargetResponseTime",
            "p95",
            "Seconds",
            alb,
        ),
        MetricDefinition(
            "alb_target_4xx",
            "AWS/ApplicationELB",
            "HTTPCode_Target_4XX_Count",
            "Sum",
            "Count",
            alb,
            True,
        ),
        MetricDefinition(
            "alb_target_5xx",
            "AWS/ApplicationELB",
            "HTTPCode_Target_5XX_Count",
            "Sum",
            "Count",
            alb,
            True,
        ),
        MetricDefinition(
            "alb_elb_4xx",
            "AWS/ApplicationELB",
            "HTTPCode_ELB_4XX_Count",
            "Sum",
            "Count",
            alb,
            True,
        ),
        MetricDefinition(
            "alb_elb_5xx",
            "AWS/ApplicationELB",
            "HTTPCode_ELB_5XX_Count",
            "Sum",
            "Count",
            alb,
            True,
        ),
        MetricDefinition(
            "api_cpu", "AWS/ECS", "CPUUtilization", "Maximum", "Percent", api
        ),
        MetricDefinition(
            "api_memory", "AWS/ECS", "MemoryUtilization", "Maximum", "Percent", api
        ),
        MetricDefinition(
            "worker_running_tasks",
            "ECS/ContainerInsights",
            "RunningTaskCount",
            "Average",
            "Count",
            worker,
        ),
        MetricDefinition(
            "worker_cpu", "AWS/ECS", "CPUUtilization", "Maximum", "Percent", worker
        ),
        MetricDefinition(
            "source_queue_sent",
            "AWS/SQS",
            "NumberOfMessagesSent",
            "Sum",
            "Count",
            source_queue,
            True,
        ),
        MetricDefinition(
            "source_queue_visible",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            "Maximum",
            "Count",
            source_queue,
            True,
        ),
        MetricDefinition(
            "source_queue_in_flight",
            "AWS/SQS",
            "ApproximateNumberOfMessagesNotVisible",
            "Maximum",
            "Count",
            source_queue,
            True,
        ),
        MetricDefinition(
            "source_queue_delayed",
            "AWS/SQS",
            "ApproximateNumberOfMessagesDelayed",
            "Maximum",
            "Count",
            source_queue,
            True,
        ),
        MetricDefinition(
            "source_queue_oldest_age",
            "AWS/SQS",
            "ApproximateAgeOfOldestMessage",
            "Maximum",
            "Seconds",
            source_queue,
            True,
        ),
        MetricDefinition(
            "dead_letter_queue_visible",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            "Maximum",
            "Count",
            dead_letter_queue,
            True,
        ),
        MetricDefinition(
            "simulator_cpu",
            "AWS/ECS",
            "CPUUtilization",
            "Maximum",
            "Percent",
            simulator,
        ),
        MetricDefinition(
            "simulator_memory",
            "AWS/ECS",
            "MemoryUtilization",
            "Maximum",
            "Percent",
            simulator,
        ),
        MetricDefinition(
            "rds_cpu", "AWS/RDS", "CPUUtilization", "Maximum", "Percent", rds
        ),
        MetricDefinition(
            "rds_connections", "AWS/RDS", "DatabaseConnections", "Maximum", "Count", rds
        ),
        MetricDefinition(
            "rds_freeable_memory", "AWS/RDS", "FreeableMemory", "Minimum", "Bytes", rds
        ),
        MetricDefinition(
            "rds_read_latency", "AWS/RDS", "ReadLatency", "Average", "Seconds", rds
        ),
        MetricDefinition(
            "rds_write_latency", "AWS/RDS", "WriteLatency", "Average", "Seconds", rds
        ),
        MetricDefinition(
            "rds_read_iops", "AWS/RDS", "ReadIOPS", "Average", "Count/Second", rds
        ),
        MetricDefinition(
            "rds_write_iops", "AWS/RDS", "WriteIOPS", "Average", "Count/Second", rds
        ),
    )
    if schema_version == 2:
        return definitions
    return tuple(
        definition
        for definition in definitions
        if definition.query_id != "source_queue_sent"
    )


def _floor_minute(value: datetime) -> datetime:
    timestamp = value.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(floor(timestamp / 60) * 60, UTC)


def _ceil_minute(value: datetime) -> datetime:
    timestamp = value.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(ceil(timestamp / 60) * 60, UTC)


def expected_bucket_starts(
    window_started_at: datetime,
    window_ended_at: datetime,
) -> tuple[datetime, ...]:
    """Return every UTC native bucket that overlaps the treatment window."""
    if window_ended_at <= window_started_at:
        raise AwsElasticityCloudWatchError(
            "CloudWatch treatment window must be positive"
        )
    start = _floor_minute(window_started_at)
    stop = _ceil_minute(window_ended_at)
    return tuple(
        start + timedelta(seconds=offset)
        for offset in range(0, int((stop - start).total_seconds()), 60)
    )


def build_elasticity_metric_queries(
    dimensions: Mapping[str, str],
) -> list[dict[str, object]]:
    """Build native queries, explicitly filling only sparse zero semantics."""
    if not valid_async_observability_dimensions(dimensions):
        raise AwsElasticityCloudWatchError(
            "Terraform returned invalid elasticity metric dimensions"
        )
    queries: list[dict[str, object]] = []
    for definition in metric_definitions():
        metric_query = {
            "Id": (
                f"m_{definition.query_id}"
                if definition.fill_missing_with_zero
                else definition.query_id
            ),
            "MetricStat": {
                "Metric": {
                    "Namespace": definition.namespace,
                    "MetricName": definition.metric_name,
                    "Dimensions": [
                        {"Name": name, "Value": dimensions[key]}
                        for name, key in definition.dimensions
                    ],
                },
                "Period": PERIOD_SECONDS,
                "Stat": definition.statistic,
            },
            "ReturnData": not definition.fill_missing_with_zero,
        }
        queries.append(metric_query)
        if definition.fill_missing_with_zero:
            queries.append(
                {
                    "Id": definition.query_id,
                    "Expression": f"FILL(m_{definition.query_id}, 0)",
                    "ReturnData": True,
                }
            )
    return queries


def parse_elasticity_metric_response(
    response_text: str,
    *,
    test_run_id: UUID,
    window_started_at: datetime,
    window_ended_at: datetime,
    collected_at: datetime,
    schema_version: Literal[1, 2] = 2,
) -> ElasticityCloudWatchEvidence:
    """Parse and require every expected series and overlapping minute bucket."""
    try:
        document = loads(response_text)
        results = document["MetricDataResults"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsElasticityCloudWatchError(
            "CloudWatch returned invalid elasticity metric data"
        ) from error
    if not isinstance(results, list):
        raise AwsElasticityCloudWatchError(
            "CloudWatch elasticity metric results must be a list"
        )
    expected_ids = {
        definition.query_id
        for definition in metric_definitions(schema_version=schema_version)
    }
    try:
        by_id = {item["Id"]: item for item in results}
    except (KeyError, TypeError) as error:
        raise AwsElasticityCloudWatchError(
            "CloudWatch returned malformed elasticity metric results"
        ) from error
    if set(by_id) != expected_ids or len(by_id) != len(results):
        raise IncompleteElasticityCloudWatchError(
            "CloudWatch omitted or duplicated elasticity metric series"
        )
    expected_timestamps = expected_bucket_starts(
        window_started_at,
        window_ended_at,
    )
    definitions = {
        item.query_id: item
        for item in metric_definitions(schema_version=schema_version)
    }
    series = []
    for query_id in sorted(expected_ids):
        result = by_id[query_id]
        try:
            timestamps = tuple(
                datetime.fromisoformat(value) for value in result["Timestamps"]
            )
            values = tuple(float(value) for value in result["Values"])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise AwsElasticityCloudWatchError(
                "CloudWatch returned invalid elasticity datapoints"
            ) from error
        if result.get("StatusCode") != "Complete" or len(timestamps) != len(values):
            raise IncompleteElasticityCloudWatchError(
                f"CloudWatch series {query_id} is not complete"
            )
        ordered = tuple(sorted(zip(timestamps, values, strict=True)))
        if tuple(timestamp for timestamp, _value in ordered) != expected_timestamps:
            expected_set = set(expected_timestamps)
            actual_set = set(timestamps)
            missing = sorted(expected_set - actual_set)
            unexpected = sorted(actual_set - expected_set)
            interior = [
                item for item in missing if any(later > item for later in actual_set)
            ]

            def utc_list(items):
                return (
                    ", ".join(item.astimezone(UTC).isoformat() for item in items)
                    or "none"
                )

            raise IncompleteElasticityCloudWatchError(
                f"CloudWatch series {query_id} has unpublished native buckets; "
                f"missing UTC: {utc_list(missing)}; interior gaps UTC: {utc_list(interior)}; "
                f"unexpected UTC: {utc_list(unexpected)}; "
                f"duplicate timestamps: {len(timestamps) - len(actual_set)}"
            )
        definition = definitions[query_id]
        try:
            series.append(
                ElasticityCloudWatchMetricSeries(
                    query_id=query_id,
                    namespace=definition.namespace,
                    metric_name=definition.metric_name,
                    statistic=definition.statistic,
                    unit=definition.unit,
                    datapoints=tuple(
                        ElasticityCloudWatchDatapoint(
                            interval_started_at=timestamp,
                            value=value,
                        )
                        for timestamp, value in ordered
                    ),
                )
            )
        except ValidationError as error:
            raise AwsElasticityCloudWatchError(
                "CloudWatch returned invalid elasticity datapoints"
            ) from error
    try:
        return ElasticityCloudWatchEvidence(
            schema_version=schema_version,
            test_run_id=test_run_id,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            collected_at=collected_at,
            series=tuple(series),
        )
    except ValidationError as error:
        raise AwsElasticityCloudWatchError(
            "CloudWatch returned invalid elasticity evidence"
        ) from error


def collect_elasticity_cloudwatch_evidence(
    session: AwsSession,
    *,
    test_run_id: UUID,
    window_started_at: datetime,
    window_ended_at: datetime,
    runner: ProcessRunner = run_process,
    now: Now = lambda: datetime.now(UTC),
    sleeper: Sleeper = sleep,
    maximum_attempts: int = 8,
    retry_interval_seconds: float = 15,
    evidence_root: Path | None = None,
) -> ElasticityCloudWatchEvidence:
    """Retry boundedly until the dashboard and full native window are published."""
    raw_dimensions = terraform_output(
        session,
        "async_observability_dimensions",
        runner=runner,
        json_output=True,
    )
    if not isinstance(raw_dimensions, dict):
        raise AwsElasticityCloudWatchError(
            "Terraform returned invalid elasticity metric dimensions"
        )
    dimensions = raw_dimensions
    if dimensions.get("scaling_metrics_namespace") == "TrackRelay/Elasticity":
        collect_scaling_diagnostics(
            session,
            dimensions,
            window_started_at,
            now(),
            session.evidence_dir / "diagnostics" / "scaling" / str(test_run_id),
            runner=runner,
        )
    query_document = build_elasticity_metric_queries(dimensions)
    operator_status("CloudWatch: checking dashboard and native metric window")
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
        action="CloudWatch elasticity dashboard inspection",
    )
    try:
        dashboard = loads(dashboard_result.stdout)
        body = loads(dashboard["DashboardBody"])
        widgets = body["widgets"]
    except (JSONDecodeError, KeyError, TypeError) as error:
        raise AwsElasticityCloudWatchError(
            "CloudWatch returned invalid elasticity dashboard evidence"
        ) from error
    if (
        dashboard.get("DashboardName") != dimensions["dashboard_name"]
        or not isinstance(widgets, list)
        or len(widgets) != 7
        or any(widget.get("type") != "metric" for widget in widgets)
    ):
        raise AwsElasticityCloudWatchError(
            "CloudWatch dashboard differs from the elasticity contract"
        )

    query_started_at = _floor_minute(window_started_at)
    query_ended_at = _ceil_minute(window_ended_at)
    # Keep the actual partial responses, not just a summary written on success.
    # They cannot satisfy the typed complete-window contract or promote a run.
    evidence_root = evidence_root or (
        session.evidence_dir
        / "diagnostics"
        / "cloudwatch"
        / str(test_run_id)
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "request.json").write_text(
        dumps(
            {
                "test_run_id": str(test_run_id),
                "dimensions": dimensions,
                "start_time": query_started_at.isoformat(),
                "end_time": query_ended_at.isoformat(),
                "queries": query_document,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    last_error: IncompleteElasticityCloudWatchError | None = None
    for attempt in range(1, maximum_attempts + 1):
        operator_status(f"CloudWatch collection: attempt {attempt}/{maximum_attempts}")
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json"
        ) as query_file:
            query_file.write(dumps(query_document))
            query_file.flush()
            result = invoke(
                runner,
                (
                    *aws_prefix(session),
                    "cloudwatch",
                    "get-metric-data",
                    "--metric-data-queries",
                    f"file://{query_file.name}",
                    "--start-time",
                    query_started_at.isoformat(),
                    "--end-time",
                    query_ended_at.isoformat(),
                    "--scan-by",
                    "TimestampAscending",
                    "--max-datapoints",
                    "1008",
                    "--output",
                    "json",
                ),
                action="CloudWatch elasticity metric collection",
            )
        try:
            # Metric responses contain numbers/timestamps, never event payloads.
            (evidence_root / f"attempt-{attempt:02d}.json").write_text(
                result.stdout, encoding="utf-8"
            )
            evidence = parse_elasticity_metric_response(
                result.stdout,
                test_run_id=test_run_id,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                collected_at=now(),
            )
            (evidence_root / "collection-status.json").write_text(
                dumps({"complete": True, "attempt": attempt}) + "\n", encoding="utf-8"
            )
            operator_status(
                "CloudWatch evidence complete: all required native buckets present"
            )
            return evidence
        except IncompleteElasticityCloudWatchError as error:
            last_error = error
            (evidence_root / "collection-status.json").write_text(
                dumps(
                    {
                        "attempt": attempt,
                        "complete": False,
                        "collected_at": now().isoformat(),
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if attempt < maximum_attempts:
                operator_status(
                    f"CloudWatch: {error}; retrying in {retry_interval_seconds:g}s"
                )
                sleeper(retry_interval_seconds)
    raise AwsElasticityCloudWatchError(
        "CloudWatch elasticity evidence remained incomplete: "
        f"{last_error or 'no collection attempt completed'}"
    )
