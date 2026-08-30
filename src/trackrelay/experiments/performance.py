"""Prepare and run repeatable local performance experiments."""

import json
import subprocess
from argparse import ArgumentParser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, sleep
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.config import Settings
from trackrelay.database import session_factory as default_session_factory
from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.downstream.control import SimulatorMode
from trackrelay.experiments.generator import (
    DEFAULT_START_AT,
    GeneratorConfiguration,
    InputManifest,
    generate_input_manifest,
    write_input_manifest,
)
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    fetch_simulator_receipts,
    reconcile_manifest,
    write_reconciliation_report,
)
from trackrelay.experiments.scenarios import (
    complete_database_run,
    prepare_database_for_run,
)
from trackrelay.experiments.slo import (
    BaselineSLOEvaluation,
    evaluate_baseline_slo,
)
from trackrelay.models import DeliveryAttempt, Event, Partner
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

LOAD_ADAPTER_TYPE = "courier-alpha"
PositiveInteger = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
Rate = Annotated[float, Field(ge=0, le=1)]


class LoadScenario(StrEnum):
    """Downstream conditions measured under a fixed request rate."""

    HEALTHY = "healthy"
    SLOW = "slow"
    OUTAGE = "outage"


SCENARIO_MODE = {
    LoadScenario.HEALTHY: SimulatorMode.HEALTHY,
    LoadScenario.SLOW: SimulatorMode.SLOW,
    LoadScenario.OUTAGE: SimulatorMode.UNAVAILABLE,
}
SCENARIO_HTTP_STATUS = {
    LoadScenario.HEALTHY: 201,
    LoadScenario.SLOW: 201,
    LoadScenario.OUTAGE: 502,
}
SCENARIO_MANIFEST_NAME = {
    LoadScenario.HEALTHY: "healthy-baseline",
    LoadScenario.SLOW: "slow-under-load",
    LoadScenario.OUTAGE: "outage-under-load",
}


class PerformanceExperimentConfiguration(BaseModel):
    """Every input needed to repeat one downstream-under-load experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    scenario: LoadScenario
    request_rate_per_second: PositiveInteger = 5
    duration_seconds: PositiveInteger = 5
    random_seed: int = 20260806
    partner_id: str = "load-alpha"
    start_at: AwareDatetime = DEFAULT_START_AT
    resource_sample_interval_seconds: PositiveFloat = 0.25
    post_load_settle_timeout_seconds: PositiveFloat = 30
    post_load_stable_window_seconds: PositiveFloat = 2
    k6_image: str = "grafana/k6:2.1.0"
    trackrelay_api_url: str = "http://127.0.0.1:8000"
    trackrelay_api_url_for_container: str = (
        "http://host.docker.internal:8000"
    )
    downstream_url: str = "http://127.0.0.1:8001"

    @computed_field
    @property
    def expected_request_count(self) -> int:
        return self.request_rate_per_second * self.duration_seconds

    @computed_field
    @property
    def simulator_mode(self) -> SimulatorMode:
        return SCENARIO_MODE[self.scenario]

    @computed_field
    @property
    def expected_http_status_code(self) -> int:
        return SCENARIO_HTTP_STATUS[self.scenario]

    @model_validator(mode="after")
    def require_complete_shipment_histories(
        self,
    ) -> "PerformanceExperimentConfiguration":
        if (
            self.scenario is not LoadScenario.HEALTHY
            and self.expected_request_count % 5
        ):
            raise ValueError(
                "request rate times duration must be divisible by 5"
            )
        return self


class RuntimeMetricsSamples(BaseModel):
    """Raw API-process samples captured while k6 was active."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    test_run_id: UUID
    samples: tuple[RuntimeMetricsSnapshot, ...]


class RuntimeMetricsObservationFailure(BaseModel):
    """One sanitized runtime-metrics gap caused by load pressure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted_at: AwareDatetime
    phase: Literal["during-load", "post-load"]
    kind: Literal["timeout", "transport", "http-status", "invalid-response"]


class RuntimeMetricsObservationFailures(BaseModel):
    """Non-fatal observation gaps retained beside the load evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    failures: tuple[RuntimeMetricsObservationFailure, ...]


class PerformanceExperimentResult(BaseModel):
    """Derived measurements and success interpretation for one load run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    test_run_id: UUID
    configuration: PerformanceExperimentConfiguration
    k6_exit_code: int
    observed_request_count: NonNegativeInteger
    dropped_iteration_count: NonNegativeInteger
    observed_request_rate_per_second: NonNegativeFloat
    p95_response_latency_ms: NonNegativeFloat
    request_error_rate: Annotated[float, Field(ge=0, le=1)]
    process_id: PositiveInteger
    process_cpu_seconds_used: NonNegativeFloat
    process_average_cpu_cores_used: NonNegativeFloat
    process_available_cpu_count: PositiveInteger
    process_average_cpu_capacity_utilization: Rate
    maximum_python_thread_count: PositiveInteger
    gil_enabled: bool
    process_max_rss_bytes_observed: NonNegativeInteger
    host_per_cpu_average_utilization: tuple[Rate, ...]
    host_average_cpu_utilization: Rate | None
    host_minimum_memory_available_bytes: NonNegativeInteger | None
    maximum_database_connections_open: NonNegativeInteger
    maximum_database_connections_checked_out: NonNegativeInteger
    maximum_database_pool_capacity: NonNegativeInteger | None
    maximum_database_pool_utilization: Rate | None
    downstream_delivery_rate_per_second: NonNegativeFloat
    downstream_delivery_receipts_per_accepted_event: NonNegativeFloat
    reconciliation: ReconciliationReport
    slo: BaselineSLOEvaluation

    @computed_field
    @property
    def execution_valid(self) -> bool:
        return (
            self.k6_exit_code == 0
            and self.dropped_iteration_count == 0
            and self.observed_request_count
            == self.configuration.expected_request_count
        )

    @computed_field
    @property
    def complete_experiment_passed(self) -> bool:
        return self.execution_valid and self.slo.experiment_passed

    @computed_field
    @property
    def productive_throughput_per_second(self) -> float:
        """Count throughput only when every execution and SLO guardrail passes."""
        return (
            self.observed_request_rate_per_second
            if self.complete_experiment_passed
            else 0.0
        )


@dataclass(frozen=True)
class PerformanceExperimentArtifacts:
    """Paths and result produced by one performance experiment."""

    run_directory: Path
    result: PerformanceExperimentResult


def prepare_load_partner(
    partner_id: str,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
) -> None:
    """Create or validate the active Courier Alpha load-test partner."""
    if not partner_id.strip():
        raise ValueError("load partner ID must not be blank")

    with sessions.begin() as session:
        database_partner = session.get(Partner, partner_id)
        if database_partner is None:
            session.add(
                Partner(
                    id=partner_id,
                    name=f"Load test {partner_id}",
                    adapter_type=LOAD_ADAPTER_TYPE,
                    is_active=True,
                )
            )
            return

        if (
            database_partner.adapter_type != LOAD_ADAPTER_TYPE
            or not database_partner.is_active
        ):
            raise ValueError(
                "load partner must be active and use courier-alpha"
            )


def build_performance_manifest(
    configuration: PerformanceExperimentConfiguration,
    *,
    test_run_id: UUID,
) -> InputManifest:
    """Build the scenario's exact scheduled Courier Alpha request set."""
    shipment_count = (
        configuration.expected_request_count
        if configuration.scenario is LoadScenario.HEALTHY
        else configuration.expected_request_count // 5
    )
    manifest = generate_input_manifest(
        seed=configuration.random_seed,
        configuration=GeneratorConfiguration(
            partner_id=configuration.partner_id,
            shipment_count=shipment_count,
            start_at=configuration.start_at,
        ),
        test_run_id=test_run_id,
    )
    if configuration.scenario is LoadScenario.HEALTHY:
        created_events = tuple(
            event.model_copy(update={"sequence_number": sequence_number})
            for sequence_number, event in enumerate(
                (
                    event
                    for event in manifest.expected_events
                    if event.expected_status is ShipmentStatus.CREATED
                ),
                start=1,
            )
        )
        manifest = InputManifest(
            test_run_id=manifest.test_run_id,
            scenario_name="healthy-baseline",
            seed=manifest.seed,
            configuration=manifest.configuration,
            events_generated=len(created_events),
            expected_unique_events=len(created_events),
            expected_events=created_events,
            expected_final_shipments={
                event.tracking_number: ShipmentStatus.CREATED
                for event in created_events
            },
        )
    return InputManifest.model_validate(
        {
            **manifest.model_dump(),
            "scenario_name": SCENARIO_MANIFEST_NAME[configuration.scenario],
        }
    )


def _write_model(model: BaseModel, output_path: Path) -> None:
    output_path.write_text(
        f"{model.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )


def _write_json(value: object, output_path: Path) -> None:
    output_path.write_text(
        f"{json.dumps(value, indent=2)}\n",
        encoding="utf-8",
    )


def _runtime_snapshot(trackrelay_client: httpx.Client) -> RuntimeMetricsSnapshot:
    response = trackrelay_client.get(
        "/api/v1/experiments/runtime-metrics",
        timeout=1.0,
    )
    response.raise_for_status()
    return RuntimeMetricsSnapshot.model_validate(response.json())


def _optional_runtime_snapshot(
    trackrelay_client: httpx.Client,
    *,
    phase: Literal["during-load", "post-load"],
    failures: list[RuntimeMetricsObservationFailure],
) -> RuntimeMetricsSnapshot | None:
    attempted_at = datetime.now(UTC)
    try:
        return _runtime_snapshot(trackrelay_client)
    except httpx.TimeoutException:
        kind = "timeout"
    except httpx.HTTPStatusError:
        kind = "http-status"
    except httpx.TransportError:
        kind = "transport"
    except (ValidationError, ValueError):
        kind = "invalid-response"
    failures.append(
        RuntimeMetricsObservationFailure(
            attempted_at=attempted_at,
            phase=phase,
            kind=kind,
        )
    )
    return None


def build_k6_command(
    configuration: PerformanceExperimentConfiguration,
    *,
    manifest_path: Path,
    run_directory: Path,
) -> tuple[str, ...]:
    """Build the pinned, shell-free Docker invocation for one load run."""
    repository_root = Path(__file__).resolve().parents[3]
    return (
        "docker",
        "run",
        "--rm",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--env",
        (
            "TRACKRELAY_API_URL="
            f"{configuration.trackrelay_api_url_for_container}"
        ),
        "--env",
        f"LOAD_RATE={configuration.request_rate_per_second}",
        "--env",
        f"LOAD_DURATION_SECONDS={configuration.duration_seconds}",
        "--env",
        f"EXPECTED_HTTP_STATUS={configuration.expected_http_status_code}",
        "--env",
        "K6_MANIFEST_PATH=/input-manifest.json",
        "--env",
        "K6_SUMMARY_PATH=/results/k6-summary.json",
        "--volume",
        f"{repository_root / 'load'}:/scripts:ro",
        "--volume",
        f"{manifest_path.resolve()}:/input-manifest.json:ro",
        "--volume",
        f"{run_directory.resolve()}:/results",
        configuration.k6_image,
        "run",
        "/scripts/fixed-rate.js",
    )


def run_k6_with_resource_sampling(
    command: Sequence[str],
    *,
    trackrelay_client: httpx.Client,
    sample_interval_seconds: float,
    on_load_started: Callable[[], None] = lambda: None,
    on_load_ended: Callable[[], None] = lambda: None,
    observation_failures: list[RuntimeMetricsObservationFailure] | None = None,
) -> tuple[int, tuple[RuntimeMetricsSnapshot, ...]]:
    failures = observation_failures if observation_failures is not None else []
    samples = [_runtime_snapshot(trackrelay_client)]
    process = subprocess.Popen(command)
    try:
        on_load_started()
        while True:
            try:
                exit_code = process.wait(timeout=sample_interval_seconds)
                break
            except subprocess.TimeoutExpired:
                sample = _optional_runtime_snapshot(
                    trackrelay_client,
                    phase="during-load",
                    failures=failures,
                )
                if sample is not None:
                    samples.append(sample)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait()
        raise
    finally:
        on_load_ended()
    final_sample = _optional_runtime_snapshot(
        trackrelay_client,
        phase="post-load",
        failures=failures,
    )
    if final_sample is not None:
        samples.append(final_sample)
    return exit_code, tuple(samples)


def derive_performance_result(
    configuration: PerformanceExperimentConfiguration,
    *,
    test_run_id: UUID,
    k6_exit_code: int,
    k6_summary: dict[str, object],
    resource_samples: Sequence[RuntimeMetricsSnapshot],
    reconciliation: ReconciliationReport,
) -> PerformanceExperimentResult:
    """Interpret portable k6, runtime, and reconciliation evidence."""
    if not resource_samples:
        raise ValueError("at least one runtime metrics sample is required")
    process_ids = {sample.process_id for sample in resource_samples}
    if len(process_ids) != 1:
        raise ValueError("runtime samples must come from one API process")
    available_cpu_counts = {
        sample.logical_cpu_count_available for sample in resource_samples
    }
    if len(available_cpu_counts) != 1:
        raise ValueError("available CPU count changed during the experiment")
    gil_states = {sample.gil_enabled for sample in resource_samples}
    if len(gil_states) != 1:
        raise ValueError("Python GIL state changed during the experiment")
    if any(sample.database_pool is None for sample in resource_samples):
        raise ValueError("API runtime samples must include database-pool metrics")
    elapsed_seconds = (
        resource_samples[-1].captured_at
        - resource_samples[0].captured_at
    ).total_seconds()
    if elapsed_seconds <= 0:
        raise ValueError("runtime samples must span a positive duration")

    p95_response_latency_ms = _k6_metric_value(
        k6_summary,
        "http_req_duration",
        "p(95)",
    )
    request_error_rate = _k6_metric_value(
        k6_summary,
        "http_req_failed",
        "rate",
    )
    slo_evaluation = evaluate_baseline_slo(
        p95_response_latency_ms=p95_response_latency_ms,
        request_error_rate=request_error_rate,
        reconciliation_report=reconciliation,
    )
    cpu_start = resource_samples[0].process_cpu_seconds
    cpu_end = resource_samples[-1].process_cpu_seconds
    process_cpu_seconds_used = max(0, cpu_end - cpu_start)
    process_average_cpu_cores_used = (
        process_cpu_seconds_used / elapsed_seconds
    )
    process_available_cpu_count = available_cpu_counts.pop()
    host_cpu_utilizations = _host_cpu_utilizations(resource_samples)
    pool_capacity = _maximum_pool_capacity(resource_samples)
    maximum_checked_out = _maximum_checked_out_connections(resource_samples)
    return PerformanceExperimentResult(
        test_run_id=test_run_id,
        configuration=configuration,
        k6_exit_code=k6_exit_code,
        observed_request_count=int(
            _k6_metric_value(k6_summary, "http_reqs", "count")
        ),
        dropped_iteration_count=int(
            _optional_k6_metric_value(
                k6_summary,
                "dropped_iterations",
                "count",
                default=0,
            )
        ),
        observed_request_rate_per_second=_k6_metric_value(
            k6_summary,
            "http_reqs",
            "rate",
        ),
        p95_response_latency_ms=p95_response_latency_ms,
        request_error_rate=request_error_rate,
        process_id=process_ids.pop(),
        process_cpu_seconds_used=process_cpu_seconds_used,
        process_average_cpu_cores_used=process_average_cpu_cores_used,
        process_available_cpu_count=process_available_cpu_count,
        process_average_cpu_capacity_utilization=min(
            1.0,
            process_average_cpu_cores_used / process_available_cpu_count,
        ),
        maximum_python_thread_count=max(
            sample.python_thread_count for sample in resource_samples
        ),
        gil_enabled=gil_states.pop(),
        process_max_rss_bytes_observed=max(
            sample.process_max_rss_bytes for sample in resource_samples
        ),
        host_per_cpu_average_utilization=host_cpu_utilizations,
        host_average_cpu_utilization=(
            sum(host_cpu_utilizations) / len(host_cpu_utilizations)
            if host_cpu_utilizations
            else None
        ),
        host_minimum_memory_available_bytes=_minimum_available_memory(
            resource_samples
        ),
        maximum_database_connections_open=(
            _maximum_open_connections(resource_samples)
        ),
        maximum_database_connections_checked_out=maximum_checked_out,
        maximum_database_pool_capacity=pool_capacity,
        maximum_database_pool_utilization=(
            min(1.0, maximum_checked_out / pool_capacity)
            if pool_capacity
            else None
        ),
        downstream_delivery_rate_per_second=(
            reconciliation.simulator_receipts
            / configuration.duration_seconds
        ),
        downstream_delivery_receipts_per_accepted_event=(
            reconciliation.simulator_receipts / reconciliation.accepted
            if reconciliation.accepted
            else 0
        ),
        reconciliation=reconciliation,
        slo=slo_evaluation,
    )


def _k6_metric_value(
    summary: dict[str, object],
    metric_name: str,
    value_name: str,
) -> float:
    try:
        metrics = summary["metrics"]
        if not isinstance(metrics, Mapping):
            raise TypeError("metrics must be an object")
        metric = metrics[metric_name]
        if not isinstance(metric, Mapping):
            raise TypeError(f"{metric_name} must be an object")
        values = metric["values"]
        if not isinstance(values, Mapping):
            raise TypeError(f"{metric_name}.values must be an object")
        observed_value = values[value_name]
        if not isinstance(observed_value, int | float):
            raise TypeError(
                f"{metric_name}.{value_name} must be a number"
            )
        return float(observed_value)
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"k6 summary is missing {metric_name}.{value_name}"
        ) from error


def _optional_k6_metric_value(
    summary: dict[str, object],
    metric_name: str,
    value_name: str,
    *,
    default: float,
) -> float:
    """Read a metric that k6 omits when it never records a sample."""
    try:
        return _k6_metric_value(summary, metric_name, value_name)
    except ValueError:
        return default


def _maximum_checked_out_connections(
    samples: Sequence[RuntimeMetricsSnapshot],
) -> int:
    return max(
        (
            sample.database_pool.checked_out
            for sample in samples
            if sample.database_pool is not None
            and sample.database_pool.checked_out is not None
        ),
        default=0,
    )


def _maximum_open_connections(
    samples: Sequence[RuntimeMetricsSnapshot],
) -> int:
    return max(
        (
            (sample.database_pool.checked_out or 0)
            + (sample.database_pool.checked_in or 0)
            for sample in samples
            if sample.database_pool is not None
        ),
        default=0,
    )


def _maximum_pool_capacity(
    samples: Sequence[RuntimeMetricsSnapshot],
) -> int | None:
    capacities = {
        sample.database_pool.pool_size
        + sample.database_pool.max_overflow
        for sample in samples
        if sample.database_pool is not None
        and sample.database_pool.pool_size is not None
        and sample.database_pool.max_overflow is not None
    }
    if not capacities:
        return None
    if len(capacities) != 1:
        raise ValueError("database pool capacity changed during the experiment")
    return capacities.pop()


def _minimum_available_memory(
    samples: Sequence[RuntimeMetricsSnapshot],
) -> int | None:
    observations = [
        sample.host_memory_available_bytes
        for sample in samples
        if sample.host_memory_available_bytes is not None
    ]
    return min(observations) if observations else None


def _host_cpu_utilizations(
    samples: Sequence[RuntimeMetricsSnapshot],
) -> tuple[float, ...]:
    first = {
        cpu.cpu_index: cpu for cpu in samples[0].host_logical_cpu_times
    }
    last = {
        cpu.cpu_index: cpu for cpu in samples[-1].host_logical_cpu_times
    }
    if not first and not last:
        return ()
    if first.keys() != last.keys():
        raise ValueError(
            "host logical CPU identities changed during the experiment"
        )

    utilizations = []
    for cpu_index in sorted(first):
        total_delta = (
            last[cpu_index].total_seconds - first[cpu_index].total_seconds
        )
        idle_delta = (
            last[cpu_index].idle_seconds - first[cpu_index].idle_seconds
        )
        if total_delta <= 0:
            raise ValueError(
                "host CPU counters must increase during the experiment"
            )
        busy_delta = max(0.0, total_delta - max(0.0, idle_delta))
        utilizations.append(min(1.0, busy_delta / total_delta))
    return tuple(utilizations)


def _receipts_for_run(
    receipts: Sequence[NormalizedEvent],
    test_run_id: UUID,
) -> tuple[NormalizedEvent, ...]:
    return tuple(
        receipt for receipt in receipts if receipt.test_run_id == test_run_id
    )


def _database_run_counts(
    test_run_id: UUID,
    *,
    sessions: sessionmaker[Session],
) -> tuple[int, int]:
    with sessions() as session:
        event_count = session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.test_run_id == test_run_id)
        )
        delivery_attempt_count = session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .join(Event, DeliveryAttempt.event_id == Event.id)
            .where(Event.test_run_id == test_run_id)
        )
    return int(event_count or 0), int(delivery_attempt_count or 0)


def wait_for_database_run_to_settle(
    test_run_id: UUID,
    *,
    expected_request_count: int,
    timeout_seconds: float,
    stable_window_seconds: float,
    sessions: sessionmaker[Session],
    sample_interval_seconds: float = 0.25,
) -> None:
    """Let server-side work finish after k6 stops waiting for responses."""
    deadline = monotonic() + timeout_seconds
    stable_since = monotonic()
    previous_counts: tuple[int, int] | None = None
    while True:
        counts = _database_run_counts(test_run_id, sessions=sessions)
        if counts == (expected_request_count, expected_request_count):
            return
        now = monotonic()
        if counts != previous_counts:
            previous_counts = counts
            stable_since = now
        elif now - stable_since >= stable_window_seconds:
            return
        if now >= deadline:
            return
        sleep(sample_interval_seconds)


def execute_performance_experiment(
    configuration: PerformanceExperimentConfiguration,
    *,
    output_root: Path,
    trackrelay_client: httpx.Client,
    downstream_client: httpx.Client,
    sessions: sessionmaker[Session] = default_session_factory,
    test_run_id: UUID | None = None,
) -> PerformanceExperimentArtifacts:
    """Run k6 under one downstream condition and save all evidence."""
    resolved_test_run_id = test_run_id or uuid4()
    manifest = build_performance_manifest(
        configuration,
        test_run_id=resolved_test_run_id,
    )
    run_directory = (
        output_root / configuration.scenario.value / str(resolved_test_run_id)
    )
    run_directory.mkdir(parents=True, exist_ok=False)
    configuration_path = run_directory / "configuration.json"
    manifest_path = run_directory / "input-manifest.json"
    resource_samples_path = run_directory / "runtime-metrics-samples.json"
    observation_failures_path = (
        run_directory / "runtime-metrics-observation-failures.json"
    )
    k6_summary_path = run_directory / "k6-summary.json"
    simulator_receipts_path = run_directory / "simulator-receipts.json"
    database_summary_path = run_directory / "database-summary.json"
    reconciliation_path = run_directory / "reconciliation.json"
    result_path = run_directory / "experiment-result.json"

    _write_model(configuration, configuration_path)
    write_input_manifest(manifest, manifest_path)
    prepare_database_for_run(manifest, sessions=sessions)

    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": configuration.simulator_mode.value},
    )
    mode_response.raise_for_status()
    command = build_k6_command(
        configuration,
        manifest_path=manifest_path,
        run_directory=run_directory,
    )
    observation_failures: list[RuntimeMetricsObservationFailure] = []
    try:
        k6_exit_code, resource_samples = run_k6_with_resource_sampling(
            command,
            trackrelay_client=trackrelay_client,
            sample_interval_seconds=(
                configuration.resource_sample_interval_seconds
            ),
            observation_failures=observation_failures,
        )
    finally:
        reset_response = downstream_client.put(
            "/control/mode",
            json={"mode": SimulatorMode.HEALTHY.value},
        )
        reset_response.raise_for_status()

    wait_for_database_run_to_settle(
        resolved_test_run_id,
        expected_request_count=configuration.expected_request_count,
        timeout_seconds=configuration.post_load_settle_timeout_seconds,
        stable_window_seconds=(
            configuration.post_load_stable_window_seconds
        ),
        sessions=sessions,
    )
    complete_database_run(resolved_test_run_id, sessions=sessions)
    runtime_samples = RuntimeMetricsSamples(
        test_run_id=resolved_test_run_id,
        samples=resource_samples,
    )
    _write_model(runtime_samples, resource_samples_path)
    _write_model(
        RuntimeMetricsObservationFailures(
            failures=tuple(observation_failures)
        ),
        observation_failures_path,
    )

    all_simulator_receipts = fetch_simulator_receipts(
        configuration.downstream_url,
        client=downstream_client,
        test_run_id=resolved_test_run_id,
    )
    simulator_receipts = _receipts_for_run(
        all_simulator_receipts,
        resolved_test_run_id,
    )
    _write_json(
        [receipt.model_dump(mode="json") for receipt in simulator_receipts],
        simulator_receipts_path,
    )
    with sessions() as session:
        reconciliation = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )
    write_reconciliation_report(reconciliation, reconciliation_path)

    summary_response = trackrelay_client.get(
        f"/api/v1/test-runs/{resolved_test_run_id}/summary"
    )
    summary_response.raise_for_status()
    _write_json(summary_response.json(), database_summary_path)

    k6_summary: dict[str, object] = json.loads(
        k6_summary_path.read_text(encoding="utf-8")
    )
    result = derive_performance_result(
        configuration,
        test_run_id=resolved_test_run_id,
        k6_exit_code=k6_exit_code,
        k6_summary=k6_summary,
        resource_samples=resource_samples,
        reconciliation=reconciliation,
    )
    _write_model(result, result_path)
    return PerformanceExperimentArtifacts(
        run_directory=run_directory,
        result=result,
    )


def build_prepare_parser() -> ArgumentParser:
    """Describe the local load-partner preparation command."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--partner-id", default="load-alpha")
    return parser


def main() -> None:
    """Prepare the configured partner before the standalone ramp test."""
    parser = build_prepare_parser()
    arguments = parser.parse_args()
    try:
        prepare_load_partner(arguments.partner_id)
    except ValueError as error:
        parser.error(str(error))
    print(f"Load partner ready: {arguments.partner_id}")


def build_experiment_parser() -> ArgumentParser:
    """Describe a repeatable downstream-under-load experiment command."""
    settings = Settings()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        type=LoadScenario,
        choices=tuple(LoadScenario),
    )
    parser.add_argument("--rate", type=int, default=5)
    parser.add_argument("--duration-seconds", type=int, default=5)
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
        default=Path("results/performance"),
    )
    return parser


def experiment_main() -> None:
    """Run one load experiment and print its saved outcome."""
    parser = build_experiment_parser()
    arguments = parser.parse_args()
    try:
        configuration = PerformanceExperimentConfiguration(
            scenario=arguments.scenario,
            request_rate_per_second=arguments.rate,
            duration_seconds=arguments.duration_seconds,
            random_seed=arguments.seed,
            partner_id=arguments.partner_id,
            start_at=arguments.start_at,
            k6_image=arguments.k6_image,
            trackrelay_api_url=arguments.api_url,
            trackrelay_api_url_for_container=arguments.container_api_url,
            downstream_url=arguments.downstream_url,
        )
        with (
            httpx.Client(
                base_url=configuration.trackrelay_api_url,
                timeout=10.0,
            ) as api_client,
            httpx.Client(
                base_url=configuration.downstream_url,
                timeout=10.0,
            ) as downstream_client,
        ):
            artifacts = execute_performance_experiment(
                configuration,
                output_root=arguments.output_root,
                trackrelay_client=api_client,
                downstream_client=downstream_client,
            )
    except (httpx.HTTPError, OSError, ValueError, ValidationError) as error:
        parser.error(str(error))

    print(f"Performance experiment: {artifacts.run_directory}")
    print(f"Execution valid: {artifacts.result.execution_valid}")
    print(f"SLO passed: {artifacts.result.slo.slo_passed}")
    print(
        "Complete experiment passed: "
        f"{artifacts.result.complete_experiment_passed}"
    )
    if not artifacts.result.execution_valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
