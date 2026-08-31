"""Freeze the local synchronous architecture's performance envelope."""

import csv
import os
import platform
import subprocess
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
import matplotlib
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url

from trackrelay.config import Settings
from trackrelay.experiments.generator import DEFAULT_START_AT
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    PerformanceExperimentResult,
    execute_performance_experiment,
    prepare_load_partner,
)
from trackrelay.experiments.slo import (
    INITIAL_BASELINE_SLO,
    BaselineSLODefinition,
)

matplotlib.use("Agg")
from matplotlib import pyplot as plt

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Rate = Annotated[float, Field(ge=0, le=1)]
DEFAULT_RATES = (10, 25, 50, 100, 250, 500)


class LegacyBaselineConfiguration(BaseModel):
    """The versioned workload and acceptance contract for Step 8.6."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["legacy-local-baseline-v1"] = "legacy-local-baseline-v1"
    architecture: Literal["local-synchronous"] = "local-synchronous"
    downstream_condition: Literal["healthy"] = "healthy"
    workload_shape: Literal["one-created-event-per-shipment"] = (
        "one-created-event-per-shipment"
    )
    offered_rates_per_second: tuple[PositiveInteger, ...] = DEFAULT_RATES
    tier_duration_seconds: PositiveInteger = 10
    random_seed: int = 20260806
    partner_id: str = "load-alpha"
    start_at: AwareDatetime = DEFAULT_START_AT
    resource_sample_interval_seconds: Annotated[float, Field(gt=0)] = 0.25
    post_load_settle_timeout_seconds: Annotated[float, Field(gt=0)] = 30
    post_load_stable_window_seconds: Annotated[float, Field(gt=0)] = 2
    k6_image: str = "grafana/k6:2.1.0"
    trackrelay_api_url: str = "http://127.0.0.1:8000"
    trackrelay_api_url_for_container: str = (
        "http://host.docker.internal:8000"
    )
    downstream_url: str = "http://127.0.0.1:8001"
    api_process_count: PositiveInteger = 1
    downstream_process_count: PositiveInteger = 1
    api_access_log_enabled: Literal[False] = False
    downstream_access_log_enabled: Literal[False] = False
    database_server: Literal["PostgreSQL 17 (postgres:17-alpine)"] = (
        "PostgreSQL 17 (postgres:17-alpine)"
    )
    slo: BaselineSLODefinition = INITIAL_BASELINE_SLO

    @field_validator("offered_rates_per_second")
    @classmethod
    def require_strictly_increasing_rates(
        cls,
        rates: tuple[int, ...],
    ) -> tuple[int, ...]:
        if not rates:
            raise ValueError("at least one offered rate is required")
        if tuple(sorted(set(rates))) != rates:
            raise ValueError(
                "offered rates must be unique and strictly increasing"
            )
        return rates

class LegacyBaselineEnvironment(BaseModel):
    """Local hardware and software context for one measured baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    captured_at: AwareDatetime
    operating_system: str
    architecture: str
    processor: str
    logical_cpu_count: PositiveInteger
    total_memory_bytes: NonNegativeInteger | None
    python_version: str
    database_url: str


class LegacyBaselineBenchmarkDefinition(BaseModel):
    """The workload contract and environment saved before traffic starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    configuration: LegacyBaselineConfiguration
    environment: LegacyBaselineEnvironment


class LegacyBaselineRateResult(BaseModel):
    """Compact pass/fail evidence for one independently evaluated rate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    offered_rate_per_second: PositiveInteger
    test_run_id: UUID
    expected_request_count: PositiveInteger
    observed_request_count: NonNegativeInteger
    dropped_iteration_count: NonNegativeInteger
    observed_request_rate_per_second: NonNegativeFloat
    p95_response_latency_ms: NonNegativeFloat
    request_error_rate: Rate
    unaccounted_events: NonNegativeInteger
    duplicate_business_effects: NonNegativeInteger
    incorrect_final_shipment_states: NonNegativeInteger
    k6_exit_code: int
    execution_valid: bool
    slo_passed: bool
    reconciliation_invariants_passed: bool
    complete_experiment_passed: bool
    failure_reasons: tuple[str, ...]
    raw_evidence_directory: str

    @model_validator(mode="after")
    def require_consistent_pass_interpretation(
        self,
    ) -> "LegacyBaselineRateResult":
        if self.complete_experiment_passed is bool(self.failure_reasons):
            raise ValueError(
                "a passing point must have no failure reasons and a failing "
                "point must explain its failure"
            )
        return self


class LegacyBaselineSummary(BaseModel):
    """The capacity boundary derived from every independently scored point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    benchmark_name: Literal["legacy-local-baseline-v1"] = (
        "legacy-local-baseline-v1"
    )
    completed_at: AwareDatetime
    maximum_sustainable_rate_per_second: PositiveInteger | None
    first_failing_rate_per_second: PositiveInteger | None
    capacity_is_at_least_highest_tested_rate: bool
    rate_results: tuple[LegacyBaselineRateResult, ...]


def _total_memory_bytes() -> int | None:
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ("sysctl", "-n", "hw.memsize"),
                check=True,
                capture_output=True,
                text=True,
            )
            return int(completed.stdout.strip())
        except (OSError, subprocess.CalledProcessError, ValueError):
            return None

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    return int(page_size * physical_pages)


def _processor_description() -> str:
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ("sysctl", "-n", "machdep.cpu.brand_string"),
                check=True,
                capture_output=True,
                text=True,
            )
            if completed.stdout.strip():
                return completed.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return platform.processor() or "unknown"


def capture_legacy_baseline_environment(
    settings: Settings,
) -> LegacyBaselineEnvironment:
    """Capture enough local context to interpret, not normalize, the result."""
    database_url = make_url(settings.database_connection_url()).render_as_string(
        hide_password=True
    )
    return LegacyBaselineEnvironment(
        captured_at=datetime.now(UTC),
        operating_system=platform.platform(),
        architecture=platform.machine() or "unknown",
        processor=_processor_description(),
        logical_cpu_count=os.cpu_count() or 1,
        total_memory_bytes=_total_memory_bytes(),
        python_version=platform.python_version(),
        database_url=database_url,
    )


def _failure_reasons(
    result: PerformanceExperimentResult,
) -> tuple[str, ...]:
    reasons = []
    if result.k6_exit_code != 0:
        reasons.append("k6 reported a threshold or request-check failure")
    if result.observed_request_count != result.configuration.expected_request_count:
        reasons.append(
            "observed request count did not match the scheduled count"
        )
    if result.dropped_iteration_count:
        reasons.append(
            f"k6 dropped {result.dropped_iteration_count} iterations"
        )
    if not result.slo.latency_target_passed:
        reasons.append("p95 response latency reached or exceeded 500 ms")
    if not result.slo.error_rate_target_passed:
        reasons.append("request error rate reached or exceeded 1%")
    if not result.slo.accounting_target_passed:
        reasons.append("unaccounted accepted events were nonzero")
    if result.reconciliation.duplicate_business_effects:
        reasons.append("unexpected duplicate business effects were nonzero")
    if result.reconciliation.incorrect_final_shipment_states:
        reasons.append("incorrect final shipment states were nonzero")
    if (
        not result.reconciliation.invariants_passed
        and not result.reconciliation.duplicate_business_effects
        and not result.reconciliation.incorrect_final_shipment_states
        and result.reconciliation.unaccounted == 0
    ):
        reasons.append("a reconciliation invariant failed")
    return tuple(reasons)


def compact_rate_result(
    result: PerformanceExperimentResult,
    *,
    raw_evidence_directory: Path,
) -> LegacyBaselineRateResult:
    """Reduce one raw experiment result to the versioned baseline fields."""
    return LegacyBaselineRateResult(
        offered_rate_per_second=(
            result.configuration.request_rate_per_second
        ),
        test_run_id=result.test_run_id,
        expected_request_count=result.configuration.expected_request_count,
        observed_request_count=result.observed_request_count,
        dropped_iteration_count=result.dropped_iteration_count,
        observed_request_rate_per_second=(
            result.observed_request_rate_per_second
        ),
        p95_response_latency_ms=result.p95_response_latency_ms,
        request_error_rate=result.request_error_rate,
        unaccounted_events=result.reconciliation.unaccounted,
        duplicate_business_effects=(
            result.reconciliation.duplicate_business_effects
        ),
        incorrect_final_shipment_states=(
            result.reconciliation.incorrect_final_shipment_states
        ),
        k6_exit_code=result.k6_exit_code,
        execution_valid=result.execution_valid,
        slo_passed=result.slo.slo_passed,
        reconciliation_invariants_passed=(
            result.reconciliation.invariants_passed
        ),
        complete_experiment_passed=result.complete_experiment_passed,
        failure_reasons=_failure_reasons(result),
        raw_evidence_directory=str(raw_evidence_directory),
    )


def derive_legacy_baseline_summary(
    rate_results: Sequence[LegacyBaselineRateResult],
    *,
    completed_at: datetime | None = None,
) -> LegacyBaselineSummary:
    """Find the last consecutive pass before the first failed point."""
    if not rate_results:
        raise ValueError("at least one rate result is required")
    rates = tuple(point.offered_rate_per_second for point in rate_results)
    if tuple(sorted(set(rates))) != rates:
        raise ValueError("rate results must be unique and strictly increasing")

    maximum_sustainable_rate = None
    first_failing_rate = None
    envelope_open = True
    for point in rate_results:
        if envelope_open and point.complete_experiment_passed:
            maximum_sustainable_rate = point.offered_rate_per_second
        elif envelope_open:
            first_failing_rate = point.offered_rate_per_second
            envelope_open = False

    return LegacyBaselineSummary(
        completed_at=completed_at or datetime.now(UTC),
        maximum_sustainable_rate_per_second=maximum_sustainable_rate,
        first_failing_rate_per_second=first_failing_rate,
        capacity_is_at_least_highest_tested_rate=(
            first_failing_rate is None
        ),
        rate_results=tuple(rate_results),
    )


def _write_model(model: BaseModel, path: Path) -> None:
    path.write_text(f"{model.model_dump_json(indent=2)}\n", encoding="utf-8")


def write_ramp_results_csv(
    summary: LegacyBaselineSummary,
    path: Path,
) -> None:
    """Write compact tabular evidence for plotting and later comparison."""
    output = StringIO(newline="")
    fieldnames = (
        "offered_rate_per_second",
        "expected_request_count",
        "observed_request_count",
        "dropped_iteration_count",
        "observed_request_rate_per_second",
        "p95_response_latency_ms",
        "request_error_rate",
        "unaccounted_events",
        "duplicate_business_effects",
        "incorrect_final_shipment_states",
        "k6_exit_code",
        "execution_valid",
        "slo_passed",
        "reconciliation_invariants_passed",
        "complete_experiment_passed",
        "failure_reasons",
        "test_run_id",
        "raw_evidence_directory",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for point in summary.rate_results:
        row = point.model_dump(mode="json")
        row["failure_reasons"] = "; ".join(point.failure_reasons)
        writer.writerow({field: row[field] for field in fieldnames})
    path.write_text(output.getvalue(), encoding="utf-8")


def write_latency_plot(
    definition: LegacyBaselineBenchmarkDefinition,
    summary: LegacyBaselineSummary,
    path: Path,
) -> None:
    """Render the one-figure local fixed-capacity result."""
    rates = [point.offered_rate_per_second for point in summary.rate_results]
    latencies = [point.p95_response_latency_ms for point in summary.rate_results]
    colors = [
        "#16803c" if point.complete_experiment_passed else "#c4322b"
        for point in summary.rate_results
    ]

    figure, axis = plt.subplots(figsize=(10, 5.625), constrained_layout=True)
    axis.plot(rates, latencies, color="#365f91", linewidth=2, zorder=1)
    axis.scatter(rates, latencies, c=colors, s=72, zorder=2)
    axis.axhline(
        definition.configuration.slo.p95_response_latency_must_be_below_ms,
        color="#c4322b",
        linestyle="--",
        linewidth=1.5,
        label="p95 SLO: below 500 ms",
    )
    axis.set_xscale("log")
    axis.set_xticks(rates, labels=[str(rate) for rate in rates])
    axis.set_xlabel("Offered load (events/s)")
    axis.set_ylabel("p95 API response latency (ms)")
    axis.set_title("TrackRelay local synchronous performance envelope")
    axis.grid(True, which="both", alpha=0.25)
    axis.legend(loc="best")
    for rate, latency in zip(rates, latencies, strict=True):
        axis.annotate(
            f"{latency:.1f} ms",
            (rate, latency),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )
    figure.savefig(
        path,
        dpi=160,
        metadata={"Software": "TrackRelay legacy baseline runner"},
    )
    plt.close(figure)


def _memory_description(total_memory_bytes: int | None) -> str:
    if total_memory_bytes is None:
        return "unknown"
    return f"{total_memory_bytes / (1024**3):.1f} GiB"


def write_baseline_readme(
    definition: LegacyBaselineBenchmarkDefinition,
    summary: LegacyBaselineSummary,
    path: Path,
) -> None:
    """Explain the measured boundary, environment, and first violation."""
    configuration = definition.configuration
    environment = definition.environment
    capacity = summary.maximum_sustainable_rate_per_second
    if capacity is None:
        result_statement = "No configured load point passed the complete gate."
    elif summary.capacity_is_at_least_highest_tested_rate:
        result_statement = (
            f"The measured capacity is at least **{capacity} events/s**; "
            "every configured point passed."
        )
    else:
        result_statement = (
            f"The maximum sustainable offered load is **{capacity} events/s**."
        )

    if summary.first_failing_rate_per_second is None:
        first_violation = "No SLO or guardrail violation was observed."
    else:
        point = next(
            result
            for result in summary.rate_results
            if result.offered_rate_per_second
            == summary.first_failing_rate_per_second
        )
        first_violation = (
            f"The first failing point was **{point.offered_rate_per_second} "
            f"events/s**: {'; '.join(point.failure_reasons)}."
        )

    table_rows = "\n".join(
        "| "
        f"{point.offered_rate_per_second} | "
        f"{point.observed_request_rate_per_second:.1f} | "
        f"{point.p95_response_latency_ms:.1f} | "
        f"{point.request_error_rate * 100:.2f}% | "
        f"{point.dropped_iteration_count} | "
        f"{point.unaccounted_events} | "
        f"{'pass' if point.complete_experiment_passed else 'fail'} |"
        for point in summary.rate_results
    )
    content = f"""# Legacy local baseline v1

{result_statement} {first_violation}

![Latency versus offered load](latency-vs-load.png)

## Result

| Offered events/s | Observed requests/s | p95 latency | Error rate | Dropped | Unaccounted | Complete gate |
| ---: | ---: | ---: | ---: | ---: | ---: | :---: |
{table_rows}

A rate passes only when p95 latency is below {configuration.slo.p95_response_latency_must_be_below_ms:g} ms, request errors are below {configuration.slo.request_error_rate_must_be_below * 100:g}%, no scheduled requests are missing or dropped, and every reconciliation invariant passes. Capacity is the last consecutive passing rate before the first failure; a later passing diagnostic point cannot reopen the envelope.

## Environment

- Captured: `{environment.captured_at.isoformat()}`
- Operating system: `{environment.operating_system}`
- Architecture: `{environment.architecture}`
- Processor: `{environment.processor}`
- Logical CPUs: `{environment.logical_cpu_count}`
- Memory: `{_memory_description(environment.total_memory_bytes)}`
- Python: `{environment.python_version}`
- Database: `{environment.database_url}`
- Database server: `{configuration.database_server}`
- k6 image: `{configuration.k6_image}`
- API processes: `{configuration.api_process_count}`
- Downstream simulator processes: `{configuration.downstream_process_count}`
- API access log enabled: `{configuration.api_access_log_enabled}`
- Downstream access log enabled: `{configuration.downstream_access_log_enabled}`
- Workload shape: `{configuration.workload_shape}`
- Tier duration: `{configuration.tier_duration_seconds}` seconds
- Random seed: `{configuration.random_seed}`

This result describes this fixed local environment; it is not normalized into a claim about other hardware. `benchmark-definition.json`, `summary.json`, and `ramp-results.csv` are the compact machine-readable evidence. Bulky per-run manifests, k6 summaries, runtime samples, and reconciliation details remain under the ignored raw-evidence paths recorded in the CSV and summary.
"""
    path.write_text(content, encoding="utf-8")


def write_legacy_baseline_artifacts(
    definition: LegacyBaselineBenchmarkDefinition,
    summary: LegacyBaselineSummary,
    output_root: Path,
) -> None:
    """Write every compact, versionable Step 8.6 artifact."""
    output_root.mkdir(parents=True, exist_ok=True)
    _write_model(definition, output_root / "benchmark-definition.json")
    _write_model(summary, output_root / "summary.json")
    write_ramp_results_csv(summary, output_root / "ramp-results.csv")
    write_latency_plot(definition, summary, output_root / "latency-vs-load.png")
    write_baseline_readme(definition, summary, output_root / "README.md")


def execute_legacy_baseline(
    configuration: LegacyBaselineConfiguration,
    *,
    environment: LegacyBaselineEnvironment,
    output_root: Path,
    raw_output_root: Path,
    trackrelay_client: httpx.Client,
    downstream_client: httpx.Client,
) -> LegacyBaselineSummary:
    """Run and independently evaluate every configured healthy load point."""
    definition = LegacyBaselineBenchmarkDefinition(
        configuration=configuration,
        environment=environment,
    )
    prepare_load_partner(configuration.partner_id)
    rate_results = []
    for rate in configuration.offered_rates_per_second:
        experiment_configuration = PerformanceExperimentConfiguration(
            scenario=LoadScenario.HEALTHY,
            request_rate_per_second=rate,
            duration_seconds=configuration.tier_duration_seconds,
            random_seed=configuration.random_seed,
            partner_id=configuration.partner_id,
            start_at=configuration.start_at,
            resource_sample_interval_seconds=(
                configuration.resource_sample_interval_seconds
            ),
            post_load_settle_timeout_seconds=(
                configuration.post_load_settle_timeout_seconds
            ),
            post_load_stable_window_seconds=(
                configuration.post_load_stable_window_seconds
            ),
            k6_image=configuration.k6_image,
            trackrelay_api_url=configuration.trackrelay_api_url,
            trackrelay_api_url_for_container=(
                configuration.trackrelay_api_url_for_container
            ),
            downstream_url=configuration.downstream_url,
        )
        artifacts = execute_performance_experiment(
            experiment_configuration,
            output_root=raw_output_root,
            trackrelay_client=trackrelay_client,
            downstream_client=downstream_client,
        )
        rate_results.append(
            compact_rate_result(
                artifacts.result,
                raw_evidence_directory=artifacts.run_directory,
            )
        )

    summary = derive_legacy_baseline_summary(rate_results)
    write_legacy_baseline_artifacts(definition, summary, output_root)
    return summary


def _parse_rates(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(rate.strip()) for rate in value.split(","))
    except ValueError as error:
        raise ValueError("rates must be comma-separated integers") from error


def build_parser() -> ArgumentParser:
    """Describe the frozen local-baseline command."""
    settings = Settings()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rates",
        default=",".join(str(rate) for rate in DEFAULT_RATES),
    )
    parser.add_argument("--tier-duration-seconds", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--partner-id", default="load-alpha")
    parser.add_argument("--start-at", default=DEFAULT_START_AT.isoformat())
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--container-api-url",
        default="http://host.docker.internal:8000",
    )
    parser.add_argument("--downstream-url", default=settings.downstream_url)
    parser.add_argument("--k6-image", default="grafana/k6:2.1.0")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("results/legacy-baseline"),
    )
    parser.add_argument(
        "--raw-output-root",
        type=Path,
        default=Path("results/raw/legacy-baseline"),
    )
    return parser


def main() -> None:
    """Run the local baseline and print its capacity interpretation."""
    settings = Settings()
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        configuration = LegacyBaselineConfiguration(
            offered_rates_per_second=_parse_rates(arguments.rates),
            tier_duration_seconds=arguments.tier_duration_seconds,
            random_seed=arguments.seed,
            partner_id=arguments.partner_id,
            start_at=arguments.start_at,
            k6_image=arguments.k6_image,
            trackrelay_api_url=arguments.api_url,
            trackrelay_api_url_for_container=arguments.container_api_url,
            downstream_url=arguments.downstream_url,
        )
        environment = capture_legacy_baseline_environment(settings)
        with (
            httpx.Client(
                base_url=configuration.trackrelay_api_url,
                timeout=30.0,
            ) as trackrelay_client,
            httpx.Client(
                base_url=configuration.downstream_url,
                timeout=30.0,
            ) as downstream_client,
        ):
            summary = execute_legacy_baseline(
                configuration,
                environment=environment,
                output_root=arguments.output_root,
                raw_output_root=arguments.raw_output_root,
                trackrelay_client=trackrelay_client,
                downstream_client=downstream_client,
            )
    except (httpx.HTTPError, OSError, ValueError, ValidationError) as error:
        parser.error(str(error))

    print(f"Legacy baseline: {arguments.output_root}")
    if summary.maximum_sustainable_rate_per_second is None:
        print("Maximum sustainable rate: none of the configured points")
    elif summary.capacity_is_at_least_highest_tested_rate:
        print(
            "Maximum sustainable rate: at least "
            f"{summary.maximum_sustainable_rate_per_second} events/s"
        )
    else:
        print(
            "Maximum sustainable rate: "
            f"{summary.maximum_sustainable_rate_per_second} events/s"
        )
        print(
            "First failing rate: "
            f"{summary.first_failing_rate_per_second} events/s"
        )


if __name__ == "__main__":
    main()
