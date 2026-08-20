"""Tests for the frozen local legacy baseline."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.experiments.baseline import (
    LegacyBaselineBenchmarkDefinition,
    LegacyBaselineConfiguration,
    LegacyBaselineEnvironment,
    LegacyBaselineRateResult,
    derive_legacy_baseline_summary,
    write_legacy_baseline_artifacts,
)


def rate_result(
    rate: int,
    *,
    passed: bool,
) -> LegacyBaselineRateResult:
    return LegacyBaselineRateResult(
        offered_rate_per_second=rate,
        test_run_id=UUID(int=rate),
        expected_request_count=rate * 10,
        observed_request_count=rate * 10,
        dropped_iteration_count=0,
        observed_request_rate_per_second=float(rate),
        p95_response_latency_ms=100 if passed else 600,
        request_error_rate=0,
        unaccounted_events=0,
        duplicate_business_effects=0,
        incorrect_final_shipment_states=0,
        k6_exit_code=0,
        execution_valid=True,
        slo_passed=passed,
        reconciliation_invariants_passed=True,
        complete_experiment_passed=passed,
        failure_reasons=(
            ()
            if passed
            else ("p95 response latency reached or exceeded 500 ms",)
        ),
        raw_evidence_directory=f"results/raw/legacy-baseline/{rate}",
    )


def benchmark_definition() -> LegacyBaselineBenchmarkDefinition:
    return LegacyBaselineBenchmarkDefinition(
        configuration=LegacyBaselineConfiguration(
            offered_rates_per_second=(10, 25, 50),
        ),
        environment=LegacyBaselineEnvironment(
            captured_at=datetime(2026, 8, 20, tzinfo=UTC),
            operating_system="Test OS",
            architecture="test64",
            processor="Test processor",
            logical_cpu_count=8,
            total_memory_bytes=16 * 1024**3,
            python_version="3.12.0",
            database_url="postgresql+psycopg://user:***@localhost/db",
        ),
    )


def test_default_configuration_freezes_the_step_86_workload() -> None:
    configuration = LegacyBaselineConfiguration()

    assert configuration.offered_rates_per_second == (
        10,
        25,
        50,
        100,
        250,
        500,
    )
    assert configuration.tier_duration_seconds == 10
    assert configuration.downstream_condition == "healthy"
    assert configuration.workload_shape == "one-created-event-per-shipment"
    assert configuration.api_access_log_enabled is False
    assert configuration.downstream_access_log_enabled is False
    assert configuration.database_server == (
        "PostgreSQL 17 (postgres:17-alpine)"
    )
    assert configuration.slo.name == "initial-local-baseline-v1"


def test_configuration_requires_ordered_unique_rates() -> None:
    with raises(ValidationError, match="unique and strictly increasing"):
        LegacyBaselineConfiguration(offered_rates_per_second=(10, 50, 25))


def test_first_failure_closes_the_sustainable_envelope() -> None:
    summary = derive_legacy_baseline_summary(
        (
            rate_result(10, passed=True),
            rate_result(25, passed=False),
            rate_result(50, passed=True),
        ),
        completed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert summary.maximum_sustainable_rate_per_second == 10
    assert summary.first_failing_rate_per_second == 25
    assert summary.capacity_is_at_least_highest_tested_rate is False


def test_all_passing_points_produce_a_lower_bound() -> None:
    summary = derive_legacy_baseline_summary(
        (rate_result(10, passed=True), rate_result(25, passed=True)),
    )

    assert summary.maximum_sustainable_rate_per_second == 25
    assert summary.first_failing_rate_per_second is None
    assert summary.capacity_is_at_least_highest_tested_rate is True


def test_compact_artifacts_include_json_csv_plot_and_readme(
    tmp_path: Path,
) -> None:
    definition = benchmark_definition()
    summary = derive_legacy_baseline_summary(
        (
            rate_result(10, passed=True),
            rate_result(25, passed=False),
            rate_result(50, passed=False),
        ),
        completed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    write_legacy_baseline_artifacts(definition, summary, tmp_path)

    assert {path.name for path in tmp_path.iterdir()} == {
        "README.md",
        "benchmark-definition.json",
        "latency-vs-load.png",
        "ramp-results.csv",
        "summary.json",
    }
    assert (tmp_path / "latency-vs-load.png").read_bytes().startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    csv_content = (tmp_path / "ramp-results.csv").read_text(
        encoding="utf-8"
    )
    assert "offered_rate_per_second" in csv_content
    assert "25,250,250,0,25.0,600.0" in csv_content
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "maximum sustainable offered load is **10 events/s**" in readme
    assert "first failing point was **25 events/s**" in readme
