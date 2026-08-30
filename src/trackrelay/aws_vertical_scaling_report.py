"""Build the portable Stage 9.3 comparison and bottleneck report locally."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_cloudwatch import (
    CloudWatchRunEvidence,
    cloudwatch_coverage_error,
)
from trackrelay.aws_rehost import (
    AwsRehostError,
    ProcessRunner,
    require_applied_clean_revision,
    run_process,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    session_from_arguments,
    write_manifest,
)
from trackrelay.aws_vertical_scaling import (
    LoadExecutionWindow,
    VerticalScalingExperimentDefinition,
    VerticalScalingTierSummary,
    VerticalScalingTransitionEvidence,
    _derive_tier_summary,
    _load_prepared_definition,
    _tier_role,
)
from trackrelay.experiments.baseline import compact_rate_result
from trackrelay.experiments.performance import PerformanceExperimentResult
from trackrelay.experiments.rehost import (
    RehostRuntimeTimeline,
    RehostServerEvidence,
)
from trackrelay.experiments.vertical_scaling import Ec2Capacity

Rate = Annotated[float, Field(ge=0, le=1)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
BottleneckAssessment = Literal[
    "no-failed-rate-observed",
    "ec2-compute-pressure",
    "rds-pressure",
    "database-pool-pressure",
    "application-process-concurrency",
    "synchronous-downstream-waiting",
    "downstream-constraint-invalidates-attribution",
    "ambiguous",
]
ComparisonKind = Literal[
    "burstable-to-compute-optimized-migration",
    "compute-to-memory-optimized-migration",
]


class BottleneckThresholds(BaseModel):
    """Versioned, visible thresholds used only to label pressure signals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high_resource_utilization: Literal[0.85] = 0.85
    spare_resource_utilization: Literal[0.70] = 0.70
    high_database_pool_utilization: Literal[0.90] = 0.90
    downstream_wait_share_of_api_latency: Literal[0.75] = 0.75
    downstream_wait_minimum_latency_ms: Literal[100] = 100


class TierBoundaryDiagnostics(BaseModel):
    """Aligned evidence at one tier's first failure or highest tested rate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_type: str
    diagnostic_rate_per_second: int
    diagnostic_rate_passed: bool
    failure_reasons: tuple[str, ...]
    productive_throughput_per_second: NonNegativeFloat
    p95_response_latency_ms: NonNegativeFloat
    request_error_rate: Rate
    dropped_iteration_count: NonNegativeInteger
    api_process_average_cores_used: NonNegativeFloat
    api_process_available_cpu_count: int = Field(gt=0)
    api_process_cpu_capacity_utilization: Rate
    api_host_average_cpu_utilization: Rate | None
    database_pool_maximum_utilization: Rate | None
    downstream_process_average_cores_used: NonNegativeFloat
    downstream_cpu_limit_cores: Literal[1.0] = 1.0
    downstream_cpu_limit_utilization: Rate
    downstream_maximum_rss_bytes: NonNegativeInteger
    downstream_memory_limit_bytes: Literal[1073741824] = 1_073_741_824
    downstream_memory_limit_utilization: Rate
    downstream_maximum_p95_delivery_latency_ms: NonNegativeInteger
    ec2_cloudwatch_average_cpu_utilization: Rate
    ec2_cloudwatch_maximum_cpu_utilization: Rate
    ec2_minimum_cpu_credit_balance: NonNegativeFloat | None
    rds_cloudwatch_average_cpu_utilization: Rate
    rds_cloudwatch_maximum_cpu_utilization: Rate
    rds_maximum_connections: NonNegativeFloat
    rds_minimum_freeable_memory_bytes: NonNegativeFloat
    rds_maximum_read_latency_ms: NonNegativeFloat
    rds_maximum_write_latency_ms: NonNegativeFloat
    pressure_signals: tuple[str, ...]
    assessment: BottleneckAssessment
    bottleneck_assessment_publishable: bool
    interpretation: str


class TierComparisonResult(BaseModel):
    """Capacity boundary and bottleneck evidence for one EC2 treatment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_type: str
    tier_role: str
    vcpu_count: int = Field(gt=0)
    memory_mib: int = Field(gt=0)
    on_demand_linux_price_usd_per_hour: float = Field(gt=0)
    maximum_sustainable_rate_per_second: int | None
    first_failing_rate_per_second: int | None
    capacity_is_at_least_highest_tested_rate: bool
    boundary: TierBoundaryDiagnostics


class HardwareTransitionComparison(BaseModel):
    """One honest before/after comparison without overstating causality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_instance_type: str
    target_instance_type: str
    comparison_kind: ComparisonKind
    source_maximum_sustainable_rate_per_second: int | None
    target_maximum_sustainable_rate_per_second: int | None
    observed_sustainable_rate_change_per_second: int | None
    observed_sustainable_rate_ratio: NonNegativeFloat | None
    capacity_comparison_is_censored: bool
    hourly_price_ratio: NonNegativeFloat
    improvement_observed: bool | None
    interpretation: str


class VerticalScalingComparisonReport(BaseModel):
    """Portable result derived from all three frozen hardware treatments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    experiment_name: Literal["aws-synchronous-hardware-flexibility-v2"] = (
        "aws-synchronous-hardware-flexibility-v2"
    )
    generated_at: AwareDatetime
    thresholds: BottleneckThresholds
    tiers: tuple[TierComparisonResult, ...]
    transitions: tuple[HardwareTransitionComparison, ...]

    @model_validator(mode="after")
    def require_the_complete_ladder(self) -> "VerticalScalingComparisonReport":
        if tuple(tier.instance_type for tier in self.tiers) != (
            "t3.small",
            "c7i-flex.large",
            "m7i-flex.large",
        ):
            raise ValueError("comparison report must contain the frozen tier order")
        if tuple(
            (comparison.source_instance_type, comparison.target_instance_type)
            for comparison in self.transitions
        ) != (
            ("t3.small", "c7i-flex.large"),
            ("c7i-flex.large", "m7i-flex.large"),
        ):
            raise ValueError("comparison report must contain both transitions")
        return self


def _read_model[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AwsRehostError(f"invalid Stage 9.3 evidence: {path}") from error


def _safe_evidence_path(session: AwsSession, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute():
        raise AwsRehostError("Stage 9.3 evidence path must be relative")
    root = session.evidence_dir.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise AwsRehostError("Stage 9.3 evidence path escapes the session")
    return resolved


def _metric(cloudwatch: CloudWatchRunEvidence, query_id: str):
    matches = tuple(series for series in cloudwatch.series if series.query_id == query_id)
    if len(matches) != 1:
        raise AwsRehostError(f"CloudWatch evidence is missing {query_id}")
    return matches[0]


def _weighted_average(cloudwatch: CloudWatchRunEvidence, query_id: str) -> float:
    series = _metric(cloudwatch, query_id)
    weight = sum(point.load_window_overlap_seconds for point in series.datapoints)
    return sum(
        point.value * point.load_window_overlap_seconds
        for point in series.datapoints
    ) / weight


def _maximum(cloudwatch: CloudWatchRunEvidence, query_id: str) -> float:
    return max(point.value for point in _metric(cloudwatch, query_id).datapoints)


def _minimum(cloudwatch: CloudWatchRunEvidence, query_id: str) -> float:
    return min(point.value for point in _metric(cloudwatch, query_id).datapoints)


def _require_full_cloudwatch_coverage(cloudwatch: CloudWatchRunEvidence) -> None:
    coverage_error = cloudwatch_coverage_error(
        cloudwatch.series,
        load_started_at=cloudwatch.load_started_at,
        load_ended_at=cloudwatch.load_ended_at,
    )
    if coverage_error is not None:
        raise AwsRehostError(f"incomplete CloudWatch evidence: {coverage_error}")


def _downstream_process_metrics(
    timeline: RehostRuntimeTimeline,
    window: LoadExecutionWindow,
) -> tuple[float, int]:
    samples = tuple(
        sample
        for sample in timeline.downstream_samples
        if window.started_at <= sample.captured_at <= window.ended_at
    )
    if len(samples) < 2:
        raise AwsRehostError("downstream timeline does not span the load window")
    if len({sample.process_id for sample in samples}) != 1:
        raise AwsRehostError("downstream process changed during the load window")
    elapsed = (samples[-1].captured_at - samples[0].captured_at).total_seconds()
    if elapsed <= 0:
        raise AwsRehostError("downstream timeline has no elapsed time")
    cores = max(
        0.0,
        samples[-1].process_cpu_seconds - samples[0].process_cpu_seconds,
    ) / elapsed
    maximum_rss = max(sample.process_max_rss_bytes for sample in samples)
    return cores, maximum_rss


def _assessment(
    *,
    passed: bool,
    api_cores: float,
    api_latency_ms: float,
    ec2_cpu: float,
    rds_cpu: float,
    pool_utilization: float | None,
    downstream_cpu: float,
    downstream_memory: float,
    downstream_latency_ms: int,
    thresholds: BottleneckThresholds,
) -> tuple[tuple[str, ...], BottleneckAssessment, bool, str]:
    if passed:
        return (
            (),
            "no-failed-rate-observed",
            False,
            (
                "The highest tested rate passed, so this experiment did not "
                "observe the tier's first bottleneck."
            ),
        )
    high = thresholds.high_resource_utilization
    spare = thresholds.spare_resource_utilization
    downstream_constrained = downstream_cpu >= high or downstream_memory >= high
    if downstream_constrained:
        signals = tuple(
            signal
            for signal, active in (
                ("downstream-cpu", downstream_cpu >= high),
                ("downstream-memory", downstream_memory >= high),
            )
            if active
        )
        return (
            signals,
            "downstream-constraint-invalidates-attribution",
            False,
            (
                "The fixed simulator reached its resource allowance, so "
                "additional TrackRelay EC2 capacity cannot receive causal "
                "credit at this rate."
            ),
        )

    candidates = []
    if ec2_cpu >= high:
        candidates.append("ec2-compute")
    if rds_cpu >= high:
        candidates.append("rds")
    if pool_utilization is not None and (
        pool_utilization >= thresholds.high_database_pool_utilization
    ):
        candidates.append("database-pool")
    if (
        api_cores >= high
        and ec2_cpu < spare
        and rds_cpu < spare
    ):
        candidates.append("application-process-concurrency")
    if (
        downstream_latency_ms >= thresholds.downstream_wait_minimum_latency_ms
        and downstream_latency_ms
        >= api_latency_ms * thresholds.downstream_wait_share_of_api_latency
        and ec2_cpu < high
        and rds_cpu < high
    ):
        candidates.append("synchronous-downstream-waiting")

    mapping: dict[str, BottleneckAssessment] = {
        "ec2-compute": "ec2-compute-pressure",
        "rds": "rds-pressure",
        "database-pool": "database-pool-pressure",
        "application-process-concurrency": "application-process-concurrency",
        "synchronous-downstream-waiting": "synchronous-downstream-waiting",
    }
    if len(candidates) == 1:
        assessment = mapping[candidates[0]]
        return (
            tuple(candidates),
            assessment,
            True,
            (
                "The boundary evidence selects "
                f"{assessment.replace('-', ' ')} as the sole matching pressure "
                "signal under the declared thresholds."
            ),
        )
    return (
        tuple(candidates),
        "ambiguous",
        False,
        (
            "The boundary evidence does not isolate one bottleneck; preserve "
            "the measurements as diagnostic evidence and avoid a causal claim."
        ),
    )


def _capacity_for(
    definition: VerticalScalingExperimentDefinition,
    instance_type: str,
) -> Ec2Capacity:
    capacities = (
        definition.capacity_selection.economical_baseline,
        definition.capacity_selection.compute_optimized,
        definition.capacity_selection.memory_optimized,
    )
    try:
        return next(
            capacity
            for capacity in capacities
            if capacity.instance_type == instance_type
        )
    except StopIteration as error:
        raise AwsRehostError("missing frozen EC2 capacity") from error


def _boundary_diagnostics(
    session: AwsSession,
    *,
    summary: VerticalScalingTierSummary,
    thresholds: BottleneckThresholds,
) -> TierBoundaryDiagnostics:
    diagnostic = next(
        (
            result
            for result in summary.rate_results
            if result.offered_rate_per_second
            == summary.first_failing_rate_per_second
        ),
        summary.rate_results[-1],
    )
    run_directory = _safe_evidence_path(
        session,
        diagnostic.raw_evidence_directory,
    )
    expected_suffix = Path(
        "vertical-scaling",
        "tiers",
        summary.instance_type,
        "rates",
        str(diagnostic.offered_rate_per_second),
        str(diagnostic.test_run_id),
    )
    if run_directory != (session.evidence_dir / expected_suffix).resolve():
        raise AwsRehostError("rate evidence directory differs from its identity")
    performance = _read_model(
        run_directory / "performance-result.json",
        PerformanceExperimentResult,
    )
    window = _read_model(run_directory / "load-window.json", LoadExecutionWindow)
    timeline = _read_model(
        run_directory / "deployment-runtime-timeline.json",
        RehostRuntimeTimeline,
    )
    server = _read_model(
        run_directory / "server-evidence.json",
        RehostServerEvidence,
    )
    cloudwatch = _read_model(
        run_directory / "cloudwatch-metrics.json",
        CloudWatchRunEvidence,
    )
    identities = {
        diagnostic.test_run_id,
        performance.test_run_id,
        window.test_run_id,
        timeline.test_run_id,
        server.point.test_run_id,
        server.reconciliation.test_run_id,
        cloudwatch.test_run_id,
    }
    if len(identities) != 1:
        raise AwsRehostError("boundary evidence test-run identities differ")
    if (
        performance.configuration.request_rate_per_second
        != diagnostic.offered_rate_per_second
        or server.point.request_rate_per_second
        != diagnostic.offered_rate_per_second
        or cloudwatch.instance_type != summary.instance_type
        or cloudwatch.load_started_at != window.started_at
        or cloudwatch.load_ended_at != window.ended_at
    ):
        raise AwsRehostError("boundary evidence differs from its hardware point")
    _require_full_cloudwatch_coverage(cloudwatch)
    regenerated_compact = compact_rate_result(
        performance,
        raw_evidence_directory=Path(diagnostic.raw_evidence_directory),
    )
    if regenerated_compact != diagnostic:
        raise AwsRehostError("compact boundary result differs from raw evidence")

    downstream_cores, downstream_rss = _downstream_process_metrics(
        timeline,
        window,
    )
    downstream_cpu_utilization = min(1.0, downstream_cores)
    downstream_memory_utilization = min(1.0, downstream_rss / 1_073_741_824)
    downstream_latency = max(
        (
            interval.p95_latency_ms
            for interval in server.downstream_delivery_intervals
        ),
        default=0,
    )
    ec2_average_cpu = _weighted_average(cloudwatch, "ec2_cpu") / 100
    rds_average_cpu = _weighted_average(cloudwatch, "rds_cpu") / 100
    signals, assessment, publishable, interpretation = _assessment(
        passed=diagnostic.complete_experiment_passed,
        api_cores=performance.process_average_cpu_cores_used,
        api_latency_ms=performance.p95_response_latency_ms,
        ec2_cpu=ec2_average_cpu,
        rds_cpu=rds_average_cpu,
        pool_utilization=performance.maximum_database_pool_utilization,
        downstream_cpu=downstream_cpu_utilization,
        downstream_memory=downstream_memory_utilization,
        downstream_latency_ms=downstream_latency,
        thresholds=thresholds,
    )
    credit_balance = (
        _minimum(cloudwatch, "ec2_cpu_credit_balance")
        if summary.instance_type == "t3.small"
        else None
    )
    return TierBoundaryDiagnostics(
        instance_type=summary.instance_type,
        diagnostic_rate_per_second=diagnostic.offered_rate_per_second,
        diagnostic_rate_passed=diagnostic.complete_experiment_passed,
        failure_reasons=diagnostic.failure_reasons,
        productive_throughput_per_second=(
            performance.productive_throughput_per_second
        ),
        p95_response_latency_ms=performance.p95_response_latency_ms,
        request_error_rate=performance.request_error_rate,
        dropped_iteration_count=performance.dropped_iteration_count,
        api_process_average_cores_used=performance.process_average_cpu_cores_used,
        api_process_available_cpu_count=performance.process_available_cpu_count,
        api_process_cpu_capacity_utilization=(
            performance.process_average_cpu_capacity_utilization
        ),
        api_host_average_cpu_utilization=performance.host_average_cpu_utilization,
        database_pool_maximum_utilization=(
            performance.maximum_database_pool_utilization
        ),
        downstream_process_average_cores_used=downstream_cores,
        downstream_cpu_limit_utilization=downstream_cpu_utilization,
        downstream_maximum_rss_bytes=downstream_rss,
        downstream_memory_limit_utilization=downstream_memory_utilization,
        downstream_maximum_p95_delivery_latency_ms=downstream_latency,
        ec2_cloudwatch_average_cpu_utilization=ec2_average_cpu,
        ec2_cloudwatch_maximum_cpu_utilization=(
            _maximum(cloudwatch, "ec2_cpu") / 100
        ),
        ec2_minimum_cpu_credit_balance=credit_balance,
        rds_cloudwatch_average_cpu_utilization=rds_average_cpu,
        rds_cloudwatch_maximum_cpu_utilization=(
            _maximum(cloudwatch, "rds_cpu") / 100
        ),
        rds_maximum_connections=_maximum(cloudwatch, "rds_connections"),
        rds_minimum_freeable_memory_bytes=_minimum(
            cloudwatch,
            "rds_freeable_memory",
        ),
        rds_maximum_read_latency_ms=(
            _maximum(cloudwatch, "rds_read_latency") * 1000
        ),
        rds_maximum_write_latency_ms=(
            _maximum(cloudwatch, "rds_write_latency") * 1000
        ),
        pressure_signals=signals,
        assessment=assessment,
        bottleneck_assessment_publishable=publishable,
        interpretation=interpretation,
    )


def _load_tier(
    session: AwsSession,
    *,
    definition: VerticalScalingExperimentDefinition,
    instance_type: str,
    thresholds: BottleneckThresholds,
) -> TierComparisonResult:
    summary_path = (
        session.evidence_dir
        / "vertical-scaling"
        / "tiers"
        / instance_type
        / "summary.json"
    )
    summary = _read_model(summary_path, VerticalScalingTierSummary)
    expected_rates = definition.controls.workload.offered_rates_per_second
    observed_rates = tuple(
        result.offered_rate_per_second for result in summary.rate_results
    )
    stopped_at_first_failure = (
        bool(summary.rate_results)
        and not summary.rate_results[-1].complete_experiment_passed
        and all(
            result.complete_experiment_passed
            for result in summary.rate_results[:-1]
        )
    )
    completed_candidate_ladder = (
        observed_rates == expected_rates
        and all(
            result.complete_experiment_passed
            for result in summary.rate_results
        )
    )
    if (
        summary.instance_type != instance_type
        or summary.tier_role != _tier_role(instance_type)
        or observed_rates != expected_rates[: len(observed_rates)]
        or not (stopped_at_first_failure or completed_candidate_ladder)
    ):
        raise AwsRehostError("tier summary differs from the frozen experiment")
    if _derive_tier_summary(
        instance_type=instance_type,
        rate_results=summary.rate_results,
        completed_at=summary.completed_at,
    ) != summary:
        raise AwsRehostError("tier capacity boundary differs from its rate results")
    capacity = _capacity_for(definition, instance_type)
    boundary = _boundary_diagnostics(
        session,
        summary=summary,
        thresholds=thresholds,
    )
    if boundary.api_process_available_cpu_count != capacity.vcpu_count:
        raise AwsRehostError("runtime CPU count differs from the frozen hardware tier")
    return TierComparisonResult(
        instance_type=instance_type,
        tier_role=summary.tier_role,
        vcpu_count=capacity.vcpu_count,
        memory_mib=capacity.memory_mib,
        on_demand_linux_price_usd_per_hour=float(
            capacity.on_demand_linux_price_usd_per_hour
        ),
        maximum_sustainable_rate_per_second=(
            summary.maximum_sustainable_rate_per_second
        ),
        first_failing_rate_per_second=summary.first_failing_rate_per_second,
        capacity_is_at_least_highest_tested_rate=(
            summary.capacity_is_at_least_highest_tested_rate
        ),
        boundary=boundary,
    )


def _transition_comparison(
    source: TierComparisonResult,
    target: TierComparisonResult,
    *,
    kind: ComparisonKind,
) -> HardwareTransitionComparison:
    source_rate = source.maximum_sustainable_rate_per_second
    target_rate = target.maximum_sustainable_rate_per_second
    change = (
        target_rate - source_rate
        if source_rate is not None and target_rate is not None
        else None
    )
    ratio = (
        target_rate / source_rate
        if source_rate is not None
        and source_rate > 0
        and target_rate is not None
        else None
    )
    censored = (
        source.capacity_is_at_least_highest_tested_rate
        or target.capacity_is_at_least_highest_tested_rate
    )
    improvement = None if change is None or censored else change > 0
    if censored:
        interpretation = (
            "At least one tier passed the highest tested rate, so the observed "
            "ratio is a boundary comparison rather than an exact capacity ratio."
        )
    elif improvement:
        interpretation = (
            "The target sustained a higher guarded rate under unchanged "
            "application and RDS controls."
        )
    else:
        interpretation = (
            "The target did not sustain a higher guarded rate; additional "
            "hardware did not improve the measured envelope."
        )
    return HardwareTransitionComparison(
        source_instance_type=source.instance_type,
        target_instance_type=target.instance_type,
        comparison_kind=kind,
        source_maximum_sustainable_rate_per_second=source_rate,
        target_maximum_sustainable_rate_per_second=target_rate,
        observed_sustainable_rate_change_per_second=change,
        observed_sustainable_rate_ratio=ratio,
        capacity_comparison_is_censored=censored,
        hourly_price_ratio=(
            target.on_demand_linux_price_usd_per_hour
            / source.on_demand_linux_price_usd_per_hour
        ),
        improvement_observed=improvement,
        interpretation=interpretation,
    )


def _validate_transition_evidence(
    session: AwsSession,
    manifest: dict[str, object],
) -> None:
    scaling = manifest["vertical_scaling"]
    records = scaling.get("transitions")
    expected = (
        ("t3.small", "c7i-flex.large"),
        ("c7i-flex.large", "m7i-flex.large"),
    )
    if not isinstance(records, list) or len(records) != 2:
        raise AwsRehostError("both Stage 9.3 transitions must be complete")
    for record, (source, target) in zip(records, expected, strict=True):
        if not isinstance(record, dict) or (
            record.get("source_instance_type"),
            record.get("target_instance_type"),
        ) != (source, target):
            raise AwsRehostError("Stage 9.3 transition order is invalid")
        evidence_path = record.get("evidence")
        if not isinstance(evidence_path, str):
            raise AwsRehostError("Stage 9.3 transition evidence path is missing")
        evidence = _read_model(
            _safe_evidence_path(session, evidence_path),
            VerticalScalingTransitionEvidence,
        )
        if (
            evidence.source_instance_type,
            evidence.target_instance_type,
        ) != (source, target):
            raise AwsRehostError("Stage 9.3 transition evidence identity differs")


def _percentage(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: VerticalScalingComparisonReport) -> str:
    """Render the compact human-readable companion to the JSON evidence."""
    lines = [
        "# Stage 9.3 hardware-flexibility comparison",
        "",
        f"Generated at `{report.generated_at.isoformat()}`.",
        "",
        "## Hardware tiers",
        "",
        "| Tier | Role | Guarded capacity | First failure | Boundary p95 | EC2 CPU | API cores | RDS CPU | Pool | Downstream CPU | Assessment |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for tier in report.tiers:
        boundary = tier.boundary
        capacity = (
            "none"
            if tier.maximum_sustainable_rate_per_second is None
            else str(tier.maximum_sustainable_rate_per_second)
        )
        if tier.capacity_is_at_least_highest_tested_rate:
            capacity = f">={capacity}"
        lines.append(
            "| "
            f"`{tier.instance_type}` | {tier.tier_role} | {capacity} | "
            f"{tier.first_failing_rate_per_second or 'none'} | "
            f"{boundary.p95_response_latency_ms:.1f} ms | "
            f"{_percentage(boundary.ec2_cloudwatch_average_cpu_utilization)} | "
            f"{boundary.api_process_average_cores_used:.2f} | "
            f"{_percentage(boundary.rds_cloudwatch_average_cpu_utilization)} | "
            f"{_percentage(boundary.database_pool_maximum_utilization)} | "
            f"{_percentage(boundary.downstream_cpu_limit_utilization)} | "
            f"{boundary.assessment} |"
        )
    lines.extend(
        (
            "",
            "## Transitions",
            "",
            "| Comparison | Kind | Guarded-rate change | Observed ratio | Price ratio | Censored |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        )
    )
    for comparison in report.transitions:
        ratio = (
            "n/a"
            if comparison.observed_sustainable_rate_ratio is None
            else f"{comparison.observed_sustainable_rate_ratio:.2f}x"
        )
        change = (
            "n/a"
            if comparison.observed_sustainable_rate_change_per_second is None
            else f"{comparison.observed_sustainable_rate_change_per_second:+d}/s"
        )
        lines.append(
            f"| `{comparison.source_instance_type}` -> "
            f"`{comparison.target_instance_type}` | "
            f"{comparison.comparison_kind} | {change} | {ratio} | "
            f"{comparison.hourly_price_ratio:.2f}x | "
            f"{'yes' if comparison.capacity_comparison_is_censored else 'no'} |"
        )
    lines.extend(("", "## Bottleneck interpretations", ""))
    for tier in report.tiers:
        lines.extend(
            (
                f"### `{tier.instance_type}`",
                "",
                tier.boundary.interpretation,
                "",
                "Pressure signals: "
                + (", ".join(tier.boundary.pressure_signals) or "none"),
                "",
                "Bottleneck assessment status: "
                + (
                    "publishable under the declared thresholds"
                    if tier.boundary.bottleneck_assessment_publishable
                    else "diagnostic only"
                ),
                "",
            )
        )
    lines.extend(
        (
            (
                "All tiers expose two vCPUs. The first transition changes from "
                "burstable baseline compute to non-burstable compute-optimized "
                "hardware. The second keeps the processor generation and vCPU "
                "count fixed while changing from a compute-optimized to a "
                "memory-optimized profile. These are cloud hardware-flexibility "
                "comparisons, not an autoscaling elasticity result."
            ),
            "",
        )
    )
    return "\n".join(lines)


def generate_vertical_scaling_report(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerticalScalingComparisonReport:
    """Validate all completed evidence and write JSON plus Markdown locally."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    scaling = manifest.get("vertical_scaling")
    if (
        manifest.get("status") != "vertical_scaling_tier_collected"
        or not isinstance(scaling, dict)
        or scaling.get("current_tier") != "m7i-flex.large"
        or scaling.get("completed_tiers")
        != ["t3.small", "c7i-flex.large", "m7i-flex.large"]
    ):
        raise AwsRehostError("all three Stage 9.3 tiers must be complete")
    definition, _ = _load_prepared_definition(
        session,
        manifest=manifest,
        revision=revision,
        require_current_incomplete=False,
    )
    _validate_transition_evidence(session, manifest)
    thresholds = BottleneckThresholds()
    try:
        tiers = tuple(
            _load_tier(
                session,
                definition=definition,
                instance_type=instance_type,
                thresholds=thresholds,
            )
            for instance_type in definition.controls.tier_order
        )
        report = VerticalScalingComparisonReport(
            generated_at=now(),
            thresholds=thresholds,
            tiers=tiers,
            transitions=(
                _transition_comparison(
                    tiers[0],
                    tiers[1],
                    kind="burstable-to-compute-optimized-migration",
                ),
                _transition_comparison(
                    tiers[1],
                    tiers[2],
                    kind="compute-to-memory-optimized-migration",
                ),
            ),
        )
    except ValueError as error:
        raise AwsRehostError(
            "Stage 9.3 report evidence is internally inconsistent"
        ) from error
    report_root = session.evidence_dir / "vertical-scaling" / "report"
    report_root.mkdir(parents=True, exist_ok=False)
    json_path = report_root / "comparison-report.json"
    markdown_path = report_root / "comparison-report.md"
    json_path.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    scaling["report"] = {
        "json": str(json_path.relative_to(session.evidence_dir)),
        "markdown": str(markdown_path.relative_to(session.evidence_dir)),
    }
    manifest["status"] = "vertical_scaling_reported"
    write_manifest(session, manifest)
    return report


def build_parser() -> ArgumentParser:
    """Build the local-only report command."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    return parser


def generate_from_arguments(arguments: Namespace) -> VerticalScalingComparisonReport:
    """Build the shared session object and generate its report."""
    return generate_vertical_scaling_report(session_from_arguments(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the complete local report without contacting AWS."""
    try:
        report = generate_from_arguments(build_parser().parse_args(argv))
    except (AwsRehostError, AwsSessionError) as error:
        raise SystemExit(f"AWS vertical-scaling report failed: {error}") from error
    print("generated the Stage 9.3 comparison and bottleneck report")
    for comparison in report.transitions:
        ratio = comparison.observed_sustainable_rate_ratio
        ratio_text = "n/a" if ratio is None else f"{ratio:.2f}x"
        print(
            f"{comparison.source_instance_type} -> "
            f"{comparison.target_instance_type}: "
            f"{ratio_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
