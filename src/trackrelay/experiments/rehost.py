"""Prepare and reconcile a rehost workload inside its private network."""

from argparse import ArgumentParser
from base64 import b64encode
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from gzip import compress
from math import ceil
from pathlib import Path
from time import monotonic, sleep
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.config import Settings
from trackrelay.database import session_factory as default_session_factory
from trackrelay.downstream.control import SimulatorMode
from trackrelay.experiments.generator import DEFAULT_START_AT, InputManifest
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    build_performance_manifest,
    prepare_load_partner,
    wait_for_database_run_to_settle,
)
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    fetch_simulator_receipts,
    reconcile_manifest,
)
from trackrelay.experiments.scenarios import (
    complete_database_run,
    prepare_database_for_run,
)
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Shipment,
    TestRun,
)
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
EVIDENCE_PREFIX = "TRACKRELAY_REHOST_EVIDENCE="
RUNTIME_EVIDENCE_PREFIX = "TRACKRELAY_RUNTIME_EVIDENCE="
RUNTIME_READY_PREFIX = "TRACKRELAY_RUNTIME_SAMPLING_READY="
RESET_EVIDENCE_PREFIX = "TRACKRELAY_RESET_EVIDENCE="
RUNTIME_SAMPLE_INTERVAL_SECONDS = 5
K6_PROCESS_STARTUP_ALLOWANCE_SECONDS = 120
K6_GRACEFUL_STOP_SECONDS = 10
RUNTIME_SAMPLER_SCHEDULING_SLACK_SECONDS = 15
RUNTIME_SAMPLING_TIMEOUT_MARGIN_SECONDS = (
    K6_PROCESS_STARTUP_ALLOWANCE_SECONDS
    + K6_GRACEFUL_STOP_SECONDS
    + RUNTIME_SAMPLE_INTERVAL_SECONDS
    + RUNTIME_SAMPLER_SCHEDULING_SLACK_SECONDS
)
RUNTIME_SAMPLE_REQUEST_TIMEOUT_SECONDS = 1.0
RUNTIME_READY_RETRY_INTERVAL_SECONDS = 1
RUNTIME_READY_MAX_ATTEMPTS = 5


class RehostWorkloadPoint(BaseModel):
    """Identity and frozen inputs for one healthy rehost load point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    request_rate_per_second: PositiveInteger
    duration_seconds: PositiveInteger
    random_seed: int = 20260806
    partner_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    start_at: AwareDatetime = DEFAULT_START_AT
    post_load_settle_timeout_seconds: Annotated[float, Field(gt=0)] = 30
    post_load_stable_window_seconds: Annotated[float, Field(gt=0)] = 2

    def performance_configuration(self) -> PerformanceExperimentConfiguration:
        """Translate the portable point into the shared workload contract."""
        return PerformanceExperimentConfiguration(
            scenario=LoadScenario.HEALTHY,
            request_rate_per_second=self.request_rate_per_second,
            duration_seconds=self.duration_seconds,
            random_seed=self.random_seed,
            partner_id=self.partner_id,
            start_at=self.start_at,
            post_load_settle_timeout_seconds=(
                self.post_load_settle_timeout_seconds
            ),
            post_load_stable_window_seconds=(
                self.post_load_stable_window_seconds
            ),
        )

    def manifest(self) -> InputManifest:
        """Regenerate exactly the manifest used by the external driver."""
        return build_performance_manifest(
            self.performance_configuration(),
            test_run_id=self.test_run_id,
        )


class RehostServerEvidence(BaseModel):
    """Compact private-side evidence safe to return through SSM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    point: RehostWorkloadPoint
    reconciliation: ReconciliationReport
    downstream_delivery_intervals: tuple["DownstreamDeliveryInterval", ...] = ()


class DownstreamDeliveryInterval(BaseModel):
    """One UTC interval of persisted downstream delivery outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_started_at: AwareDatetime
    interval_seconds: Literal[5] = 5
    attempt_count: PositiveInteger
    delivered_count: NonNegativeInteger
    http_error_count: NonNegativeInteger
    transport_error_count: NonNegativeInteger
    p95_latency_ms: NonNegativeInteger
    maximum_latency_ms: NonNegativeInteger


class RuntimeSamplingFailure(BaseModel):
    """Sanitized evidence that one process could not be sampled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["api", "downstream"]
    kind: Literal[
        "timeout",
        "transport-error",
        "http-status",
        "invalid-response",
    ]
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None

    @model_validator(mode="after")
    def require_status_only_for_http_failures(
        self,
    ) -> "RuntimeSamplingFailure":
        if (self.status_code is not None) != (self.kind == "http-status"):
            raise ValueError("only HTTP-status failures have a status code")
        return self


class DeploymentRuntimeSample(BaseModel):
    """One attempt to sample both private processes without hiding gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted_at: AwareDatetime
    api: RuntimeMetricsSnapshot | None = None
    downstream: RuntimeMetricsSnapshot | None = None
    failures: tuple[RuntimeSamplingFailure, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_attempt_time(cls, value: Any) -> Any:
        """Accept schema-v2 paired samples without attempt timestamps."""
        if not isinstance(value, dict) or value.get("attempted_at") is not None:
            return value
        captured_times = []
        for target in ("api", "downstream"):
            snapshot = value.get(target)
            if isinstance(snapshot, RuntimeMetricsSnapshot):
                captured_times.append(snapshot.captured_at)
            elif (
                isinstance(snapshot, dict)
                and snapshot.get("captured_at") is not None
            ):
                captured_times.append(snapshot["captured_at"])
        if captured_times:
            return {**value, "attempted_at": max(captured_times)}
        return value

    @model_validator(mode="after")
    def require_one_outcome_per_target(self) -> "DeploymentRuntimeSample":
        failure_targets = tuple(failure.target for failure in self.failures)
        if len(failure_targets) != len(set(failure_targets)):
            raise ValueError("runtime sample repeats a target failure")
        for target in ("api", "downstream"):
            has_snapshot = getattr(self, target) is not None
            has_failure = target in failure_targets
            if has_snapshot == has_failure:
                raise ValueError(
                    f"runtime sample must contain one {target} outcome"
                )
        return self

    @property
    def complete(self) -> bool:
        """Return whether both process snapshots were captured."""
        return self.api is not None and self.downstream is not None


class RehostRuntimeTimeline(BaseModel):
    """Compact private process timeline sampled throughout one load point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2, 3, 4] = 4
    test_run_id: UUID
    sample_interval_seconds: Literal[5] = 5
    sampling_timeout_seconds: PositiveInteger
    samples: tuple[DeploymentRuntimeSample, ...]

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_duration_name(cls, value: Any) -> Any:
        """Read schema-v2/v3 evidence written before stop handshakes."""
        if (
            isinstance(value, dict)
            and "sampling_timeout_seconds" not in value
            and "sampling_duration_seconds" in value
        ):
            migrated = dict(value)
            migrated["sampling_timeout_seconds"] = migrated.pop(
                "sampling_duration_seconds"
            )
            return migrated
        return value

    @model_validator(mode="after")
    def require_ordered_samples(self) -> "RehostRuntimeTimeline":
        if not self.samples:
            raise ValueError("runtime timeline must contain samples")
        attempted_times = tuple(
            sample.attempted_at for sample in self.samples
        )
        if attempted_times != tuple(sorted(attempted_times)):
            raise ValueError(
                "runtime sampling attempts must be chronological"
            )
        api_times = tuple(
            sample.api.captured_at
            for sample in self.samples
            if sample.api is not None
        )
        downstream_times = tuple(
            sample.downstream.captured_at
            for sample in self.samples
            if sample.downstream is not None
        )
        if not any(sample.complete for sample in self.samples):
            raise ValueError("runtime timeline must contain a complete sample")
        if api_times != tuple(sorted(api_times)):
            raise ValueError("API runtime samples must be chronological")
        if downstream_times != tuple(sorted(downstream_times)):
            raise ValueError("downstream runtime samples must be chronological")
        return self

    @property
    def coverage_started_at(self) -> datetime:
        """Return when both process timelines first became ready."""
        first = next(sample for sample in self.samples if sample.complete)
        assert first.api is not None
        assert first.downstream is not None
        return max(first.api.captured_at, first.downstream.captured_at)

    @property
    def coverage_ended_at(self) -> datetime:
        """Return the last instant at which both processes were attempted."""
        return self.samples[-1].attempted_at

    @property
    def api_samples(self) -> tuple[RuntimeMetricsSnapshot, ...]:
        """Return successful API snapshots in chronological order."""
        return tuple(
            sample.api for sample in self.samples if sample.api is not None
        )

    @property
    def downstream_samples(self) -> tuple[RuntimeMetricsSnapshot, ...]:
        """Return successful downstream snapshots in chronological order."""
        return tuple(
            sample.downstream
            for sample in self.samples
            if sample.downstream is not None
        )

    @property
    def sampling_failures(self) -> tuple[RuntimeSamplingFailure, ...]:
        """Return explicit process sampling gaps in observation order."""
        return tuple(
            failure for sample in self.samples for failure in sample.failures
        )


class ExperimentTableCounts(BaseModel):
    """Synthetic rows present before or after a treatment reset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_attempts: NonNegativeInteger
    events: NonNegativeInteger
    shipments: NonNegativeInteger
    test_runs: NonNegativeInteger


class RehostExperimentResetEvidence(BaseModel):
    """Proof that only synthetic treatment state was cleared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    completed_at: AwareDatetime
    database_rows_removed: ExperimentTableCounts
    database_rows_remaining: ExperimentTableCounts
    downstream_receipts_removed: NonNegativeInteger
    downstream_receipts_remaining: Literal[0] = 0

    @model_validator(mode="after")
    def require_an_empty_database(self) -> "RehostExperimentResetEvidence":
        if any(self.database_rows_remaining.model_dump().values()):
            raise ValueError("experiment reset left synthetic database rows")
        return self


def _experiment_table_counts(session: Session) -> ExperimentTableCounts:
    return ExperimentTableCounts(
        delivery_attempts=session.scalar(
            select(func.count()).select_from(DeliveryAttempt)
        )
        or 0,
        events=session.scalar(select(func.count()).select_from(Event)) or 0,
        shipments=session.scalar(select(func.count()).select_from(Shipment)) or 0,
        test_runs=session.scalar(select(func.count()).select_from(TestRun)) or 0,
    )


def reset_rehost_experiment_state(
    *,
    sessions: sessionmaker[Session] = default_session_factory,
    downstream_client: httpx.Client,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RehostExperimentResetEvidence:
    """Clear synthetic rows and receipts while preserving configuration."""
    with sessions.begin() as session:
        before = _experiment_table_counts(session)
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text(
                    "TRUNCATE TABLE delivery_outbox, delivery_attempts, events, "
                    "shipments, test_runs"
                )
            )
        else:
            for model in (
                DeliveryOutboxEntry,
                DeliveryAttempt,
                Event,
                Shipment,
                TestRun,
            ):
                session.execute(delete(model))
        after = _experiment_table_counts(session)

    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": SimulatorMode.HEALTHY.value},
    )
    mode_response.raise_for_status()
    clear_response = downstream_client.delete("/control/events")
    clear_response.raise_for_status()
    cleared_receipts = clear_response.json().get("cleared_event_count")
    remaining_response = downstream_client.get("/events")
    remaining_response.raise_for_status()
    remaining_receipts = remaining_response.json()
    if not isinstance(cleared_receipts, int) or cleared_receipts < 0:
        raise ValueError("downstream reset returned an invalid receipt count")
    if remaining_receipts != []:
        raise ValueError("downstream reset left synthetic receipts")
    return RehostExperimentResetEvidence(
        completed_at=now(),
        database_rows_removed=before,
        database_rows_remaining=after,
        downstream_receipts_removed=cleared_receipts,
    )


def prepare_rehost_workload_point(
    point: RehostWorkloadPoint,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
    downstream_client: httpx.Client,
) -> None:
    """Create private database state and require a healthy simulator."""
    manifest = point.manifest()
    mode_response = downstream_client.put(
        "/control/mode",
        json={"mode": SimulatorMode.HEALTHY.value},
    )
    mode_response.raise_for_status()
    prepare_load_partner(point.partner_id, sessions=sessions)
    prepare_database_for_run(manifest, sessions=sessions)


def collect_rehost_workload_evidence(
    point: RehostWorkloadPoint,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
    downstream_client: httpx.Client,
) -> RehostServerEvidence:
    """Settle and reconcile evidence without exposing private services."""
    manifest = point.manifest()
    configuration = point.performance_configuration()
    wait_for_database_run_to_settle(
        point.test_run_id,
        expected_request_count=configuration.expected_request_count,
        timeout_seconds=configuration.post_load_settle_timeout_seconds,
        stable_window_seconds=(
            configuration.post_load_stable_window_seconds
        ),
        sessions=sessions,
    )
    complete_database_run(point.test_run_id, sessions=sessions)
    simulator_receipts = fetch_simulator_receipts(
        str(downstream_client.base_url),
        client=downstream_client,
        test_run_id=point.test_run_id,
    )
    with sessions() as session:
        reconciliation = reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )
    delivery_intervals = collect_downstream_delivery_intervals(
        point.test_run_id,
        sessions=sessions,
    )
    return RehostServerEvidence(
        point=point,
        reconciliation=reconciliation,
        downstream_delivery_intervals=delivery_intervals,
    )


def _as_aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def collect_downstream_delivery_intervals(
    test_run_id: UUID,
    *,
    sessions: sessionmaker[Session] = default_session_factory,
) -> tuple[DownstreamDeliveryInterval, ...]:
    """Aggregate persisted attempts into aligned five-second UTC intervals."""
    with sessions() as session:
        attempts = tuple(
            session.execute(
                select(
                    DeliveryAttempt.started_at,
                    DeliveryAttempt.result,
                    DeliveryAttempt.latency_ms,
                )
                .join(Event, DeliveryAttempt.event_id == Event.id)
                .where(Event.test_run_id == test_run_id)
                .order_by(DeliveryAttempt.started_at.asc())
            ).tuples()
        )

    buckets: dict[int, list[tuple[object, int]]] = {}
    for started_at, result, latency_ms in attempts:
        timestamp = int(_as_aware_utc(started_at).timestamp())
        bucket_start = timestamp - timestamp % RUNTIME_SAMPLE_INTERVAL_SECONDS
        buckets.setdefault(bucket_start, []).append((result, latency_ms))

    intervals = []
    for bucket_start, observations in sorted(buckets.items()):
        latencies = sorted(latency for _, latency in observations)
        p95_index = max(0, ceil(len(latencies) * 0.95) - 1)
        result_values = [str(result) for result, _ in observations]
        intervals.append(
            DownstreamDeliveryInterval(
                interval_started_at=datetime.fromtimestamp(
                    bucket_start,
                    tz=UTC,
                ),
                attempt_count=len(observations),
                delivered_count=result_values.count("delivered"),
                http_error_count=result_values.count("http_error"),
                transport_error_count=result_values.count("transport_error"),
                p95_latency_ms=latencies[p95_index],
                maximum_latency_ms=latencies[-1],
            )
        )
    return tuple(intervals)


def _capture_runtime_snapshot(
    client: httpx.Client,
    path: str,
    *,
    target: Literal["api", "downstream"],
) -> tuple[RuntimeMetricsSnapshot | None, RuntimeSamplingFailure | None]:
    """Capture one endpoint without letting observer failure hide overload."""
    try:
        response = client.get(path)
        response.raise_for_status()
        return RuntimeMetricsSnapshot.model_validate(response.json()), None
    except httpx.TimeoutException:
        kind: Literal[
            "timeout",
            "transport-error",
            "http-status",
            "invalid-response",
        ] = "timeout"
        status_code = None
    except httpx.HTTPStatusError as error:
        kind = "http-status"
        status_code = error.response.status_code
    except httpx.RequestError:
        kind = "transport-error"
        status_code = None
    except ValueError:
        kind = "invalid-response"
        status_code = None
    return None, RuntimeSamplingFailure(
        target=target,
        kind=kind,
        status_code=status_code,
    )


def _capture_deployment_runtime_sample(
    *,
    api_client: httpx.Client,
    downstream_client: httpx.Client,
    now: Callable[[], datetime],
) -> DeploymentRuntimeSample:
    """Attempt both endpoints independently so one cannot mask the other."""
    attempted_at = now()
    api, api_failure = _capture_runtime_snapshot(
        api_client,
        "/api/v1/experiments/runtime-metrics",
        target="api",
    )
    downstream, downstream_failure = _capture_runtime_snapshot(
        downstream_client,
        "/experiments/runtime-metrics",
        target="downstream",
    )
    return DeploymentRuntimeSample(
        attempted_at=attempted_at,
        api=api,
        downstream=downstream,
        failures=tuple(
            failure
            for failure in (api_failure, downstream_failure)
            if failure is not None
        ),
    )


def sample_rehost_runtime_timeline(
    point: RehostWorkloadPoint,
    *,
    api_client: httpx.Client,
    downstream_client: httpx.Client,
    sampling_timeout_seconds: int | None = None,
    sleeper=sleep,
    monotonic_clock: Callable[[], float] = monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_ready: Callable[[datetime], None] = lambda _captured_at: None,
    stop_requested: Callable[[], bool] = lambda: False,
) -> RehostRuntimeTimeline:
    """Sample until the controller stops the run or its safety bound expires."""
    sampling_timeout = (
        point.duration_seconds
        if sampling_timeout_seconds is None
        else sampling_timeout_seconds
    )
    if sampling_timeout < point.duration_seconds:
        raise ValueError("runtime sampling timeout cannot precede scheduled load")
    samples = []
    for readiness_attempt in range(RUNTIME_READY_MAX_ATTEMPTS):
        ready_sample = _capture_deployment_runtime_sample(
            api_client=api_client,
            downstream_client=downstream_client,
            now=now,
        )
        samples.append(ready_sample)
        if ready_sample.complete:
            assert ready_sample.api is not None
            assert ready_sample.downstream is not None
            on_ready(
                max(
                    ready_sample.api.captured_at,
                    ready_sample.downstream.captured_at,
                )
            )
            break
        if readiness_attempt < RUNTIME_READY_MAX_ATTEMPTS - 1:
            sleeper(RUNTIME_READY_RETRY_INTERVAL_SECONDS)
    else:
        raise RuntimeError("runtime endpoints did not become ready")

    deadline = monotonic_clock() + sampling_timeout
    while True:
        remaining_seconds = deadline - monotonic_clock()
        if remaining_seconds <= 0:
            raise RuntimeError("runtime sampler did not receive the stop signal")
        sleeper(min(RUNTIME_SAMPLE_INTERVAL_SECONDS, remaining_seconds))
        samples.append(
            _capture_deployment_runtime_sample(
                api_client=api_client,
                downstream_client=downstream_client,
                now=now,
            )
        )
        if stop_requested():
            break
        if monotonic_clock() >= deadline:
            raise RuntimeError("runtime sampler did not receive the stop signal")
    return RehostRuntimeTimeline(
        test_run_id=point.test_run_id,
        sampling_timeout_seconds=sampling_timeout,
        samples=tuple(samples),
    )


def encoded_evidence(evidence: RehostServerEvidence) -> str:
    """Encode one compact JSON object as an unambiguous output line."""
    encoded = b64encode(evidence.model_dump_json().encode("utf-8")).decode(
        "ascii"
    )
    return f"{EVIDENCE_PREFIX}{encoded}"


def encoded_runtime_evidence(evidence: RehostRuntimeTimeline) -> str:
    """Compress a private timeline beneath the SSM standard-output limit."""
    compressed = compress(
        evidence.model_dump_json().encode("utf-8"),
        compresslevel=9,
        mtime=0,
    )
    encoded = b64encode(compressed).decode("ascii")
    if len(encoded) > 20_000:
        raise ValueError("compressed runtime evidence exceeds the safety limit")
    return f"{RUNTIME_EVIDENCE_PREFIX}{encoded}"


def encoded_reset_evidence(evidence: RehostExperimentResetEvidence) -> str:
    """Encode compact treatment-reset proof for SSM collection."""
    encoded = b64encode(evidence.model_dump_json().encode("utf-8")).decode(
        "ascii"
    )
    return f"{RESET_EVIDENCE_PREFIX}{encoded}"


def build_parser() -> ArgumentParser:
    """Build the private helper's deliberately narrow command line."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("reset")
    for action in ("prepare", "collect", "sample-runtime"):
        action_parser = subparsers.add_parser(action)
        action_parser.add_argument("--test-run-id", type=UUID, required=True)
        action_parser.add_argument("--rate", type=int, required=True)
        action_parser.add_argument("--duration-seconds", type=int, required=True)
        action_parser.add_argument("--sampling-timeout-seconds", type=int)
        action_parser.add_argument("--seed", type=int, required=True)
        action_parser.add_argument("--partner-id", required=True)
        action_parser.add_argument("--start-at", required=True)
        action_parser.add_argument(
            "--settle-timeout-seconds",
            type=float,
            required=True,
        )
        action_parser.add_argument(
            "--stable-window-seconds",
            type=float,
            required=True,
        )
        if action == "sample-runtime":
            action_parser.add_argument("--stop-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run within the deployed Compose network, normally through SSM."""
    arguments = build_parser().parse_args(argv)
    settings = Settings()
    if arguments.action == "reset":
        with httpx.Client(
            base_url=settings.downstream_url,
            timeout=settings.downstream_timeout_seconds,
        ) as downstream_client:
            evidence = reset_rehost_experiment_state(
                downstream_client=downstream_client,
            )
        print(encoded_reset_evidence(evidence))
        return 0

    point = RehostWorkloadPoint(
        test_run_id=arguments.test_run_id,
        request_rate_per_second=arguments.rate,
        duration_seconds=arguments.duration_seconds,
        random_seed=arguments.seed,
        partner_id=arguments.partner_id,
        start_at=arguments.start_at,
        post_load_settle_timeout_seconds=arguments.settle_timeout_seconds,
        post_load_stable_window_seconds=arguments.stable_window_seconds,
    )
    if arguments.action == "sample-runtime":
        with (
            httpx.Client(
                base_url="http://api:8000",
                timeout=RUNTIME_SAMPLE_REQUEST_TIMEOUT_SECONDS,
            ) as api_client,
            httpx.Client(
                base_url=settings.downstream_url,
                timeout=RUNTIME_SAMPLE_REQUEST_TIMEOUT_SECONDS,
            ) as downstream_client,
        ):
            timeline = sample_rehost_runtime_timeline(
                point,
                api_client=api_client,
                downstream_client=downstream_client,
                sampling_timeout_seconds=(
                    arguments.sampling_timeout_seconds
                ),
                stop_requested=lambda: Path(
                    arguments.stop_file
                ).is_file(),
                on_ready=lambda captured_at: print(
                    f"{RUNTIME_READY_PREFIX}{captured_at.isoformat()}",
                    flush=True,
                ),
            )
        print(encoded_runtime_evidence(timeline))
        return 0

    with httpx.Client(
        base_url=settings.downstream_url,
        timeout=settings.downstream_timeout_seconds,
    ) as downstream_client:
        if arguments.action == "prepare":
            prepare_rehost_workload_point(
                point,
                downstream_client=downstream_client,
            )
            print("TRACKRELAY_REHOST_PREPARED")
        else:
            evidence = collect_rehost_workload_evidence(
                point,
                downstream_client=downstream_client,
            )
            print(encoded_evidence(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
