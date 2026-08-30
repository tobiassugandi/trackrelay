"""Collect UTC-aligned EC2 and RDS evidence for one AWS load point."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil, floor, isfinite
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_rehost import (
    INSTANCE_ID_PATTERN,
    RDS_IDENTIFIER_PATTERN,
    AwsRehostError,
    ProcessRunner,
    aws_prefix,
    invoke,
    run_process,
)
from trackrelay.aws_session import AwsSession
from trackrelay.experiments.vertical_scaling import ALLOWED_INSTANCE_TYPES

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
MetricSource = Literal["ec2", "rds"]
Statistic = Literal["Average", "Minimum", "Sum"]
Sleeper = Callable[[float], None]
Now = Callable[[], datetime]


class CloudWatchDatapoint(BaseModel):
    """One published value and its actual overlap with the load window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_started_at: AwareDatetime
    value: float
    load_window_overlap_seconds: NonNegativeFloat

    @model_validator(mode="after")
    def require_a_finite_value(self) -> "CloudWatchDatapoint":
        if not isfinite(self.value):
            raise ValueError("CloudWatch datapoint values must be finite")
        return self


class CloudWatchMetricSeries(BaseModel):
    """One AWS metric at its honest native experiment resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    source: MetricSource
    metric_name: str
    statistic: Statistic
    unit: str
    period_seconds: Literal[60, 300]
    datapoints: tuple[CloudWatchDatapoint, ...]

    @model_validator(mode="after")
    def require_ordered_overlapping_datapoints(self) -> "CloudWatchMetricSeries":
        timestamps = tuple(point.interval_started_at for point in self.datapoints)
        if timestamps != tuple(sorted(set(timestamps))):
            raise ValueError("CloudWatch datapoints must be unique and ordered")
        if any(
            point.load_window_overlap_seconds > self.period_seconds
            for point in self.datapoints
        ):
            raise ValueError("datapoint overlap cannot exceed its metric period")
        return self


class CloudWatchRunEvidence(BaseModel):
    """Resource evidence aligned to one identified Stage 9.3 load point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    instance_type: str
    load_started_at: AwareDatetime
    load_ended_at: AwareDatetime
    collected_at: AwareDatetime
    series: tuple[CloudWatchMetricSeries, ...]

    @model_validator(mode="after")
    def require_complete_treatment_evidence(self) -> "CloudWatchRunEvidence":
        if self.instance_type not in ALLOWED_INSTANCE_TYPES:
            raise ValueError("CloudWatch evidence has an unapproved instance type")
        if self.load_ended_at <= self.load_started_at:
            raise ValueError("CloudWatch evidence needs a positive load window")
        expected = {
            definition.query_id
            for definition in metric_definitions(self.instance_type)
        }
        observed = {metric.query_id for metric in self.series}
        if observed != expected or len(observed) != len(self.series):
            raise ValueError("CloudWatch evidence is missing or duplicates metrics")
        if any(not metric.datapoints for metric in self.series):
            raise ValueError("every CloudWatch metric must overlap the load window")
        return self


class MetricDefinition(NamedTuple):
    """Stable translation from an evidence name to one AWS metric query."""

    query_id: str
    source: MetricSource
    metric_name: str
    statistic: Statistic
    unit: str
    period_seconds: Literal[60, 300]


BASE_METRICS = (
    MetricDefinition(
        "ec2_cpu", "ec2", "CPUUtilization", "Average", "Percent", 60
    ),
    MetricDefinition(
        "ec2_network_in", "ec2", "NetworkIn", "Sum", "Bytes", 60
    ),
    MetricDefinition(
        "ec2_network_out", "ec2", "NetworkOut", "Sum", "Bytes", 60
    ),
    MetricDefinition(
        "rds_cpu", "rds", "CPUUtilization", "Average", "Percent", 60
    ),
    MetricDefinition(
        "rds_connections", "rds", "DatabaseConnections", "Average", "Count", 60
    ),
    MetricDefinition(
        "rds_freeable_memory", "rds", "FreeableMemory", "Minimum", "Bytes", 60
    ),
    MetricDefinition(
        "rds_read_latency", "rds", "ReadLatency", "Average", "Seconds", 60
    ),
    MetricDefinition(
        "rds_write_latency", "rds", "WriteLatency", "Average", "Seconds", 60
    ),
    MetricDefinition(
        "rds_read_iops", "rds", "ReadIOPS", "Average", "Count/Second", 60
    ),
    MetricDefinition(
        "rds_write_iops", "rds", "WriteIOPS", "Average", "Count/Second", 60
    ),
)
BURST_CREDIT_METRICS = (
    MetricDefinition(
        "ec2_cpu_credit_usage", "ec2", "CPUCreditUsage", "Sum", "Count", 300
    ),
    MetricDefinition(
        "ec2_cpu_credit_balance", "ec2", "CPUCreditBalance", "Average", "Count", 300
    ),
)


def metric_definitions(instance_type: str) -> tuple[MetricDefinition, ...]:
    """Return credits only for the frozen burstable treatment."""
    if instance_type not in ALLOWED_INSTANCE_TYPES:
        raise AwsRehostError("cannot query an unapproved EC2 treatment")
    if instance_type == "t3.small":
        return BASE_METRICS + BURST_CREDIT_METRICS
    return BASE_METRICS


def _floor_time(value: datetime, period_seconds: int) -> datetime:
    timestamp = value.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(
        floor(timestamp / period_seconds) * period_seconds,
        UTC,
    )


def _ceil_time(value: datetime, period_seconds: int) -> datetime:
    timestamp = value.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(
        ceil(timestamp / period_seconds) * period_seconds,
        UTC,
    )


def build_cloudwatch_query(
    *,
    instance_id: str,
    rds_identifier: str,
    instance_type: str,
    load_started_at: datetime,
    load_ended_at: datetime,
) -> dict[str, object]:
    """Build one batched query without persisting infrastructure identifiers."""
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("invalid EC2 instance ID for CloudWatch evidence")
    if RDS_IDENTIFIER_PATTERN.fullmatch(rds_identifier) is None:
        raise AwsRehostError("invalid RDS identifier for CloudWatch evidence")
    if load_ended_at <= load_started_at:
        raise AwsRehostError("CloudWatch load window must be positive")
    definitions = metric_definitions(instance_type)
    longest_period = max(definition.period_seconds for definition in definitions)
    queries = []
    for definition in definitions:
        namespace = "AWS/EC2" if definition.source == "ec2" else "AWS/RDS"
        dimension_name = (
            "InstanceId"
            if definition.source == "ec2"
            else "DBInstanceIdentifier"
        )
        dimension_value = (
            instance_id if definition.source == "ec2" else rds_identifier
        )
        queries.append(
            {
                "Id": definition.query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": definition.metric_name,
                        "Dimensions": [
                            {"Name": dimension_name, "Value": dimension_value}
                        ],
                    },
                    "Period": definition.period_seconds,
                    "Stat": definition.statistic,
                },
                "ReturnData": True,
            }
        )
    return {
        "MetricDataQueries": queries,
        "StartTime": _floor_time(load_started_at, longest_period).isoformat(),
        "EndTime": _ceil_time(load_ended_at, longest_period).isoformat(),
        "ScanBy": "TimestampAscending",
    }


def parse_cloudwatch_response(
    response: str,
    *,
    test_run_id: UUID,
    instance_type: str,
    load_started_at: datetime,
    load_ended_at: datetime,
    collected_at: datetime,
) -> CloudWatchRunEvidence:
    """Validate AWS output and retain only datapoints overlapping the load."""
    try:
        document = json.loads(response)
        results = document["MetricDataResults"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise AwsRehostError("CloudWatch returned invalid metric data") from error
    if not isinstance(results, list):
        raise AwsRehostError("CloudWatch metric results must be a list")
    definitions = {
        definition.query_id: definition
        for definition in metric_definitions(instance_type)
    }
    series = []
    for result in results:
        try:
            query_id = result["Id"]
            timestamps = result["Timestamps"]
            values = result["Values"]
            status = result["StatusCode"]
        except (KeyError, TypeError) as error:
            raise AwsRehostError("CloudWatch returned an incomplete metric result") from error
        if query_id not in definitions or status != "Complete":
            raise AwsRehostError("CloudWatch returned unknown or partial metric data")
        if (
            not isinstance(timestamps, list)
            or not isinstance(values, list)
            or len(timestamps) != len(values)
        ):
            raise AwsRehostError("CloudWatch timestamps and values do not align")
        definition = definitions[query_id]
        points = []
        for timestamp_text, value in zip(timestamps, values, strict=True):
            try:
                interval_start = datetime.fromisoformat(timestamp_text)
                interval_end = interval_start + timedelta(
                    seconds=definition.period_seconds
                )
                overlap = max(
                    0.0,
                    (
                        min(interval_end, load_ended_at)
                        - max(interval_start, load_started_at)
                    ).total_seconds(),
                )
                numeric_value = float(value)
            except (AttributeError, TypeError, ValueError) as error:
                raise AwsRehostError("CloudWatch returned an invalid datapoint") from error
            if overlap > 0:
                points.append(
                    CloudWatchDatapoint(
                        interval_started_at=interval_start,
                        value=numeric_value,
                        load_window_overlap_seconds=overlap,
                    )
                )
        series.append(
            CloudWatchMetricSeries(
                query_id=query_id,
                source=definition.source,
                metric_name=definition.metric_name,
                statistic=definition.statistic,
                unit=definition.unit,
                period_seconds=definition.period_seconds,
                datapoints=tuple(
                    sorted(points, key=lambda point: point.interval_started_at)
                ),
            )
        )
    try:
        return CloudWatchRunEvidence(
            test_run_id=test_run_id,
            instance_type=instance_type,
            load_started_at=load_started_at,
            load_ended_at=load_ended_at,
            collected_at=collected_at,
            series=tuple(sorted(series, key=lambda metric: metric.query_id)),
        )
    except ValueError as error:
        raise AwsRehostError("CloudWatch evidence is incomplete") from error


def collect_cloudwatch_evidence(
    session: AwsSession,
    *,
    instance_id: str,
    rds_identifier: str,
    test_run_id: UUID,
    load_started_at: datetime,
    load_ended_at: datetime,
    runner: ProcessRunner = run_process,
    sleeper: Sleeper = sleep,
    now: Now = lambda: datetime.now(UTC),
    maximum_attempts: PositiveInteger = 40,
    retry_interval_seconds: PositiveInteger = 15,
) -> CloudWatchRunEvidence:
    """Poll one bounded batched query until every required metric is published."""
    query = build_cloudwatch_query(
        instance_id=instance_id,
        rds_identifier=rds_identifier,
        instance_type=session.rehost_instance_type,
        load_started_at=load_started_at,
        load_ended_at=load_ended_at,
    )
    for attempt in range(maximum_attempts):
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="trackrelay-cloudwatch-",
            suffix=".json",
        ) as query_file:
            query_file.write(json.dumps(query))
            query_file.flush()
            output = invoke(
                runner,
                (
                    *aws_prefix(session),
                    "cloudwatch",
                    "get-metric-data",
                    "--cli-input-json",
                    f"file://{query_file.name}",
                    "--output",
                    "json",
                    "--no-cli-pager",
                ),
                action="CloudWatch resource evidence collection",
            ).stdout
        try:
            return parse_cloudwatch_response(
                output,
                test_run_id=test_run_id,
                instance_type=session.rehost_instance_type,
                load_started_at=load_started_at,
                load_ended_at=load_ended_at,
                collected_at=now(),
            )
        except AwsRehostError:
            if attempt + 1 == maximum_attempts:
                raise AwsRehostError(
                    "CloudWatch did not publish complete aligned evidence in time"
                ) from None
            sleeper(retry_interval_seconds)
    raise AssertionError("unreachable CloudWatch polling state")
