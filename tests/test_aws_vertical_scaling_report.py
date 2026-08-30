"""Tests for the local Stage 9.3 comparison and bottleneck report."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pytest import raises

from tests.test_aws_rehost import COMMAND_ID
from tests.test_aws_vertical_scaling import (
    clean_revision_runner,
    prepare_rds_session,
)
from trackrelay.aws_cloudwatch import (
    CloudWatchDatapoint,
    CloudWatchMetricSeries,
    CloudWatchRunEvidence,
    metric_definitions,
)
from trackrelay.aws_rehost import AwsRehostError
from trackrelay.aws_session import load_manifest, write_manifest
from trackrelay.aws_vertical_scaling import (
    InfrastructureTransitionPlanEvidence,
    LoadExecutionWindow,
    VerticalScalingRateAssessment,
    VerticalScalingTierSummary,
    VerticalScalingTransitionEvidence,
    VerticalScalingTrialResult,
    VerticalScalingWarmupEvidence,
    _derive_tier_summary,
    prepare_vertical_scaling_experiment,
)
from trackrelay.aws_vertical_scaling_report import (
    generate_vertical_scaling_report,
)
from trackrelay.experiments.baseline import (
    LegacyBaselineRateResult,
    compact_rate_result,
)
from trackrelay.experiments.performance import (
    PerformanceExperimentConfiguration,
    derive_performance_result,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    DeploymentRuntimeSample,
    DownstreamDeliveryInterval,
    ExperimentTableCounts,
    RehostExperimentResetEvidence,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
    RuntimeSamplingFailure,
)
from trackrelay.runtime_metrics import (
    DatabasePoolMetrics,
    RuntimeMetricsSnapshot,
)

STARTED_AT = datetime(2026, 8, 29, 12, tzinfo=UTC)
ENDED_AT = STARTED_AT + timedelta(seconds=180)


def runtime_sample(
    *,
    captured_at: datetime,
    process_id: int,
    cpu_seconds: float,
    available_cpus: int,
    database_pool: bool,
) -> RuntimeMetricsSnapshot:
    return RuntimeMetricsSnapshot(
        captured_at=captured_at,
        process_id=process_id,
        process_cpu_seconds=cpu_seconds,
        process_max_rss_bytes=100 * 1024 * 1024,
        python_thread_count=4,
        logical_cpu_count_available=available_cpus,
        gil_enabled=True,
        host_logical_cpu_times=(),
        host_memory_total_bytes=4 * 1024**3,
        host_memory_available_bytes=3 * 1024**3,
        database_pool=(
            DatabasePoolMetrics(
                checked_out=1,
                checked_in=4,
                pool_size=5,
                overflow=0,
                max_overflow=10,
            )
            if database_pool
            else None
        ),
    )


def cloudwatch_evidence(
    *,
    test_run_id: UUID,
    instance_type: str,
    ec2_cpu_percent: float,
) -> CloudWatchRunEvidence:
    values = {
        "ec2_cpu": ec2_cpu_percent,
        "rds_cpu": 30.0,
        "rds_connections": 5.0,
        "rds_freeable_memory": 700_000_000.0,
        "rds_read_latency": 0.002,
        "rds_write_latency": 0.003,
        "ec2_cpu_credit_balance": 10.0,
    }
    series = tuple(
        CloudWatchMetricSeries(
            query_id=definition.query_id,
            source=definition.source,
            metric_name=definition.metric_name,
            statistic=definition.statistic,
            unit=definition.unit,
            period_seconds=definition.period_seconds,
            datapoints=tuple(
                CloudWatchDatapoint(
                    interval_started_at=(
                        STARTED_AT
                        + timedelta(
                            seconds=index * definition.period_seconds
                        )
                    ),
                    value=values.get(definition.query_id, 1.0),
                    load_window_overlap_seconds=min(
                        definition.period_seconds,
                        180 - index * definition.period_seconds,
                    ),
                )
                for index in range(
                    (180 + definition.period_seconds - 1)
                    // definition.period_seconds
                )
            ),
        )
        for definition in metric_definitions(instance_type)
    )
    return CloudWatchRunEvidence(
        test_run_id=test_run_id,
        instance_type=instance_type,
        load_started_at=STARTED_AT,
        load_ended_at=ENDED_AT,
        collected_at=ENDED_AT + timedelta(minutes=5),
        series=series,
    )


def write_trial_evidence(
    session,
    *,
    instance_type: str,
    test_run_id: UUID,
    ec2_cpu_percent: float,
    rate: int,
    passed: bool,
) -> LegacyBaselineRateResult:
    relative_directory = Path(
        "vertical-scaling",
        "tiers",
        instance_type,
        "rates",
        str(rate),
        str(test_run_id),
    )
    run_directory = session.evidence_dir / relative_directory
    run_directory.mkdir(parents=True)
    expected = rate * 180
    reconciliation = ReconciliationReport(
        test_run_id=test_run_id,
        generated=expected,
        accepted=expected,
        rejected=0,
        unique=expected,
        processed=expected,
        failed=0,
        pending=0,
        unaccounted=0,
        simulator_receipts=expected,
        simulator_unique_events=expected,
    )
    available_cpus = 2
    api_samples = (
        runtime_sample(
            captured_at=STARTED_AT,
            process_id=7,
            cpu_seconds=0,
            available_cpus=available_cpus,
            database_pool=True,
        ),
        runtime_sample(
            captured_at=ENDED_AT,
            process_id=7,
            cpu_seconds=180,
            available_cpus=available_cpus,
            database_pool=True,
        ),
    )
    performance = derive_performance_result(
        PerformanceExperimentConfiguration(
            scenario="healthy",
            request_rate_per_second=rate,
            duration_seconds=180,
        ),
        test_run_id=test_run_id,
        k6_exit_code=0 if passed else 99,
        k6_summary={
            "metrics": {
                "http_req_duration": {
                    "values": {"p(95)": 100 if passed else 600}
                },
                "http_req_failed": {"values": {"rate": 0}},
                "http_reqs": {"values": {"count": expected, "rate": rate}},
                "dropped_iterations": {"values": {"count": 0}},
            }
        },
        resource_samples=api_samples,
        reconciliation=reconciliation,
    )
    (run_directory / "performance-result.json").write_text(
        performance.model_dump_json(indent=2, exclude_computed_fields=True),
        encoding="utf-8",
    )
    window = LoadExecutionWindow(
        test_run_id=test_run_id,
        started_at=STARTED_AT,
        ended_at=ENDED_AT,
    )
    (run_directory / "load-window.json").write_text(
        window.model_dump_json(indent=2),
        encoding="utf-8",
    )
    downstream_samples = (
        runtime_sample(
            captured_at=STARTED_AT,
            process_id=8,
            cpu_seconds=0,
            available_cpus=available_cpus,
            database_pool=False,
        ),
        runtime_sample(
            captured_at=ENDED_AT,
            process_id=8,
            cpu_seconds=18,
            available_cpus=available_cpus,
            database_pool=False,
        ),
    )
    overload_observation = DeploymentRuntimeSample(
        attempted_at=STARTED_AT + timedelta(seconds=90),
        api=None,
        downstream=runtime_sample(
            captured_at=STARTED_AT + timedelta(seconds=90),
            process_id=8,
            cpu_seconds=9,
            available_cpus=available_cpus,
            database_pool=False,
        ),
        failures=(RuntimeSamplingFailure(target="api", kind="timeout"),),
    )
    timeline = RehostRuntimeTimeline(
        test_run_id=test_run_id,
        sampling_timeout_seconds=210,
        samples=(
            DeploymentRuntimeSample(
                api=api_samples[0],
                downstream=downstream_samples[0],
            ),
            overload_observation,
            DeploymentRuntimeSample(
                api=api_samples[1],
                downstream=downstream_samples[1],
            ),
        ),
    )
    (run_directory / "deployment-runtime-timeline.json").write_text(
        timeline.model_dump_json(indent=2),
        encoding="utf-8",
    )
    point = RehostWorkloadPoint(
        test_run_id=test_run_id,
        request_rate_per_second=rate,
        duration_seconds=180,
        partner_id="load-alpha",
    )
    server = RehostServerEvidence(
        point=point,
        reconciliation=reconciliation,
        downstream_delivery_intervals=(
            DownstreamDeliveryInterval(
                interval_started_at=STARTED_AT,
                attempt_count=expected,
                delivered_count=expected,
                http_error_count=0,
                transport_error_count=0,
                p95_latency_ms=20,
                maximum_latency_ms=30,
            ),
        ),
    )
    (run_directory / "server-evidence.json").write_text(
        server.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (run_directory / "cloudwatch-metrics.json").write_text(
        cloudwatch_evidence(
            test_run_id=test_run_id,
            instance_type=instance_type,
            ec2_cpu_percent=ec2_cpu_percent,
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    return compact_rate_result(
        performance,
        raw_evidence_directory=relative_directory,
    )


def compact_point(
    *,
    instance_type: str,
    rate: int,
    test_run_id: UUID,
    passed: bool,
) -> LegacyBaselineRateResult:
    return LegacyBaselineRateResult(
        offered_rate_per_second=rate,
        test_run_id=test_run_id,
        expected_request_count=rate * 180,
        observed_request_count=rate * 180,
        dropped_iteration_count=0,
        observed_request_rate_per_second=rate,
        p95_response_latency_ms=100 if passed else 700,
        request_error_rate=0,
        unaccounted_events=0,
        duplicate_business_effects=0,
        incorrect_final_shipment_states=0,
        k6_exit_code=0 if passed else 99,
        execution_valid=passed,
        slo_passed=passed,
        reconciliation_invariants_passed=True,
        complete_experiment_passed=passed,
        failure_reasons=() if passed else ("later point remained outside envelope",),
        raw_evidence_directory=str(
            Path(
                "vertical-scaling",
                "tiers",
                instance_type,
                "rates",
                str(rate),
                str(test_run_id),
            )
        ),
    )


def empty_reset_evidence() -> RehostExperimentResetEvidence:
    empty = ExperimentTableCounts(
        delivery_attempts=0,
        events=0,
        shipments=0,
        test_runs=0,
    )
    return RehostExperimentResetEvidence(
        completed_at=datetime(2026, 8, 29, 19, tzinfo=UTC),
        database_rows_removed=empty,
        database_rows_remaining=empty,
        downstream_receipts_removed=0,
    )


def rate_assessment(
    results: tuple[LegacyBaselineRateResult, ...],
) -> VerticalScalingRateAssessment:
    assessment = VerticalScalingRateAssessment(
        offered_rate_per_second=results[0].offered_rate_per_second,
        passed=sum(result.complete_experiment_passed for result in results) >= 2,
        trials=tuple(
            VerticalScalingTrialResult(
                trial_number=index,
                result=result,
                post_trial_reset=empty_reset_evidence(),
            )
            for index, result in enumerate(results, start=1)
        ),
    )
    for trial in assessment.trials:
        run_directory = Path(trial.result.raw_evidence_directory)
        # The caller's evidence root is not available here; write_tier persists
        # these model-identical reset files after constructing the assessment.
        assert not run_directory.is_absolute()
    return assessment


def test_rate_assessment_uses_a_two_of_three_majority(tmp_path: Path) -> None:
    session = prepare_rds_session(tmp_path)
    results = tuple(
        write_trial_evidence(
            session,
            instance_type="t3.small",
            test_run_id=UUID(int=index),
            ec2_cpu_percent=50,
            rate=10,
            passed=passed,
        )
        for index, passed in enumerate((False, True, True), start=1)
    )

    assessment = rate_assessment(results)

    assert assessment.passed is True
    assert assessment.passing_trial_count == 2
    assert assessment.failing_trial_count == 1
    assert assessment.representative_result == results[2]


def write_tier(session, *, instance_type: str, integer_offset: int) -> None:
    boundary_id = UUID(int=integer_offset + 25)
    boundary = write_trial_evidence(
        session,
        instance_type=instance_type,
        test_run_id=boundary_id,
        ec2_cpu_percent=90 if instance_type == "t3.small" else 50,
        rate=25,
        passed=False,
    )
    warmup_id = UUID(int=integer_offset + 2)
    warmup_result = LegacyBaselineRateResult(
        offered_rate_per_second=2,
        test_run_id=warmup_id,
        expected_request_count=60,
        observed_request_count=60,
        dropped_iteration_count=0,
        observed_request_rate_per_second=2,
        p95_response_latency_ms=50,
        request_error_rate=0,
        unaccounted_events=0,
        duplicate_business_effects=0,
        incorrect_final_shipment_states=0,
        k6_exit_code=0,
        execution_valid=True,
        slo_passed=True,
        reconciliation_invariants_passed=True,
        complete_experiment_passed=True,
        failure_reasons=(),
        raw_evidence_directory=str(
            Path(
                "vertical-scaling",
                "tiers",
                instance_type,
                "warmup",
                str(warmup_id),
            )
        ),
    )
    warmup_path = Path(
        "vertical-scaling",
        "tiers",
        instance_type,
        "warmup-summary.json",
    )
    full_warmup_path = session.evidence_dir / warmup_path
    full_warmup_path.parent.mkdir(parents=True, exist_ok=True)
    full_warmup_path.write_text(
        VerticalScalingWarmupEvidence(
            result=warmup_result,
            reset=empty_reset_evidence(),
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    passing_results = tuple(
        write_trial_evidence(
            session,
            instance_type=instance_type,
            test_run_id=UUID(int=integer_offset + 10 + trial),
            ec2_cpu_percent=50,
            rate=10,
            passed=True,
        )
        for trial in range(3)
    )
    failing_results = (
        write_trial_evidence(
            session,
            instance_type=instance_type,
            test_run_id=UUID(int=integer_offset + 23),
            ec2_cpu_percent=90 if instance_type == "t3.small" else 50,
            rate=25,
            passed=False,
        ),
        write_trial_evidence(
            session,
            instance_type=instance_type,
            test_run_id=UUID(int=integer_offset + 24),
            ec2_cpu_percent=90 if instance_type == "t3.small" else 50,
            rate=25,
            passed=False,
        ),
        boundary,
    )
    assessments = (
        rate_assessment(passing_results),
        rate_assessment(failing_results),
    )
    for assessment in assessments:
        assessment_path = (
            session.evidence_dir
            / "vertical-scaling"
            / "tiers"
            / instance_type
            / "rates"
            / str(assessment.offered_rate_per_second)
            / "assessment.json"
        )
        assessment_path.parent.mkdir(parents=True, exist_ok=True)
        assessment_path.write_text(
            assessment.model_dump_json(indent=2),
            encoding="utf-8",
        )
        for trial in assessment.trials:
            reset_path = (
                session.evidence_dir
                / trial.result.raw_evidence_directory
                / "post-trial-reset.json"
            )
            reset_path.write_text(
                trial.post_trial_reset.model_dump_json(indent=2),
                encoding="utf-8",
            )
    summary = _derive_tier_summary(
        instance_type=instance_type,
        warmup_evidence=str(warmup_path),
        rate_assessments=assessments,
        completed_at=datetime(2026, 8, 29, 20, tzinfo=UTC),
    )
    tier_root = session.evidence_dir / "vertical-scaling" / "tiers" / instance_type
    (tier_root / "summary.json").write_text(
        summary.model_dump_json(indent=2),
        encoding="utf-8",
    )


def write_transition(
    session,
    *,
    source: str,
    target: str,
    minute: int,
) -> str:
    empty = ExperimentTableCounts(
        delivery_attempts=0,
        events=0,
        shipments=0,
        test_runs=0,
    )
    evidence = VerticalScalingTransitionEvidence(
        source_instance_type=source,
        target_instance_type=target,
        reset=RehostExperimentResetEvidence(
            completed_at=datetime(2026, 8, 29, 19, minute, tzinfo=UTC),
            database_rows_removed=empty,
            database_rows_remaining=empty,
            downstream_receipts_removed=0,
        ),
        plan=InfrastructureTransitionPlanEvidence(
            source_instance_type=source,
            target_instance_type=target,
            plan_sha256="a" * 64,
            actions=("update",),
            changed_attributes=("instance_type",),
        ),
        applied_at=datetime(2026, 8, 29, 19, minute, tzinfo=UTC),
        validated_at=datetime(2026, 8, 29, 19, minute + 1, tzinfo=UTC),
        observed_instance_type=target,
        deployment_validation_command_id=COMMAND_ID,
    )
    relative = Path(
        "vertical-scaling",
        "transitions",
        f"{source}-to-{target}",
        "transition-evidence.json",
    )
    path = session.evidence_dir / relative
    path.parent.mkdir(parents=True)
    path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    return str(relative)


def complete_experiment_session(tmp_path: Path):
    baseline_session = prepare_rds_session(tmp_path)
    prepare_vertical_scaling_experiment(
        baseline_session,
        runner=clean_revision_runner,
        now=lambda: datetime(2026, 8, 29, 13, tzinfo=UTC),
    )
    for index, instance_type in enumerate(
        ("t3.small", "c7i-flex.large", "m7i-flex.large"),
        start=1,
    ):
        write_tier(
            baseline_session,
            instance_type=instance_type,
            integer_offset=index * 1000,
        )
    final_session = replace(
        baseline_session,
        rehost_instance_type="m7i-flex.large",
    )
    manifest = load_manifest(baseline_session)
    manifest["rehost_instance_type"] = "m7i-flex.large"
    manifest["status"] = "vertical_scaling_tier_collected"
    manifest["vertical_scaling"].update(
        {
            "current_tier": "m7i-flex.large",
            "completed_tiers": ["t3.small", "c7i-flex.large", "m7i-flex.large"],
            "transitions": [
                {
                    "source_instance_type": "t3.small",
                    "target_instance_type": "c7i-flex.large",
                    "evidence": write_transition(
                        baseline_session,
                        source="t3.small",
                        target="c7i-flex.large",
                        minute=0,
                    ),
                },
                {
                    "source_instance_type": "c7i-flex.large",
                    "target_instance_type": "m7i-flex.large",
                    "evidence": write_transition(
                        baseline_session,
                        source="c7i-flex.large",
                        target="m7i-flex.large",
                        minute=2,
                    ),
                },
            ],
        }
    )
    write_manifest(final_session, manifest)
    return final_session


def test_complete_report_aligns_all_tiers_and_stays_local(tmp_path: Path) -> None:
    session = complete_experiment_session(tmp_path)
    runner_calls = []

    def runner(arguments, input_text):
        runner_calls.append(tuple(arguments))
        return clean_revision_runner(arguments, input_text)

    report = generate_vertical_scaling_report(
        session,
        runner=runner,
        now=lambda: datetime(2026, 8, 29, 21, tzinfo=UTC),
    )

    assert tuple(tier.instance_type for tier in report.tiers) == (
        "t3.small",
        "c7i-flex.large",
        "m7i-flex.large",
    )
    assert report.tiers[0].boundary.assessment == "ec2-compute-pressure"
    assert report.tiers[0].boundary.bottleneck_assessment_publishable is True
    assert report.tiers[1].boundary.assessment == "application-process-concurrency"
    assert report.tiers[2].boundary.assessment == "application-process-concurrency"
    assert tuple(item.comparison_kind for item in report.transitions) == (
        "burstable-to-compute-optimized-migration",
        "compute-to-memory-optimized-migration",
    )
    assert report.transitions[0].observed_sustainable_rate_ratio == 1
    assert runner_calls == [
        ("git", "status", "--porcelain"),
        ("git", "rev-parse", "HEAD"),
    ]
    report_root = session.evidence_dir / "vertical-scaling" / "report"
    assert (report_root / "comparison-report.json").is_file()
    markdown = (report_root / "comparison-report.md").read_text(encoding="utf-8")
    assert "`t3.small` -> `c7i-flex.large`" in markdown
    assert "cloud hardware-flexibility comparisons" in markdown
    assert "not an autoscaling elasticity result" in markdown
    manifest = load_manifest(session)
    assert manifest["status"] == "vertical_scaling_reported"
    assert manifest["vertical_scaling"]["report"] == {
        "json": "vertical-scaling/report/comparison-report.json",
        "markdown": "vertical-scaling/report/comparison-report.md",
    }


def test_report_rejects_a_truncated_tier_without_a_failing_boundary(
    tmp_path: Path,
) -> None:
    session = complete_experiment_session(tmp_path)
    summary_path = (
        session.evidence_dir
        / "vertical-scaling"
        / "tiers"
        / "t3.small"
        / "summary.json"
    )
    summary = VerticalScalingTierSummary.model_validate_json(
        summary_path.read_text(encoding="utf-8")
    )
    truncated = _derive_tier_summary(
        instance_type="t3.small",
        warmup_evidence=summary.warmup_evidence,
        rate_assessments=summary.rate_assessments[:1],
        completed_at=summary.completed_at,
    )
    summary_path.write_text(
        truncated.model_dump_json(indent=2),
        encoding="utf-8",
    )

    with raises(AwsRehostError, match="tier summary differs"):
        generate_vertical_scaling_report(
            session,
            runner=clean_revision_runner,
        )

    assert not (session.evidence_dir / "vertical-scaling" / "report").exists()


def test_report_rejects_a_missing_transition_before_writing_output(
    tmp_path: Path,
) -> None:
    session = complete_experiment_session(tmp_path)
    manifest = load_manifest(session)
    manifest["vertical_scaling"]["transitions"].pop()
    write_manifest(session, manifest)

    with raises(AwsRehostError, match="both Stage 9.3 transitions"):
        generate_vertical_scaling_report(
            session,
            runner=clean_revision_runner,
        )

    assert not (session.evidence_dir / "vertical-scaling" / "report").exists()
