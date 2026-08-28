"""Prepare and reconcile a rehost workload inside its private network."""

from argparse import ArgumentParser
from base64 import b64encode
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from gzip import compress
from math import ceil
from time import sleep
from typing import Annotated, Literal
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
from trackrelay.models import DeliveryAttempt, Event, Shipment, TestRun
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
EVIDENCE_PREFIX = "TRACKRELAY_REHOST_EVIDENCE="
RUNTIME_EVIDENCE_PREFIX = "TRACKRELAY_RUNTIME_EVIDENCE="
RESET_EVIDENCE_PREFIX = "TRACKRELAY_RESET_EVIDENCE="
RUNTIME_SAMPLE_INTERVAL_SECONDS = 5
RUNTIME_SAMPLING_MARGIN_SECONDS = 15


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


class DeploymentRuntimeSample(BaseModel):
    """Near-simultaneous API and downstream process snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api: RuntimeMetricsSnapshot
    downstream: RuntimeMetricsSnapshot


class RehostRuntimeTimeline(BaseModel):
    """Compact private process timeline sampled throughout one load point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    test_run_id: UUID
    sample_interval_seconds: Literal[5] = 5
    sampling_duration_seconds: PositiveInteger
    samples: tuple[DeploymentRuntimeSample, ...]


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
                    "TRUNCATE TABLE delivery_attempts, events, shipments, "
                    "test_runs"
                )
            )
        else:
            for model in (DeliveryAttempt, Event, Shipment, TestRun):
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


def sample_rehost_runtime_timeline(
    point: RehostWorkloadPoint,
    *,
    api_client: httpx.Client,
    downstream_client: httpx.Client,
    sampling_duration_seconds: int | None = None,
    sleeper=sleep,
    on_ready: Callable[[], None] = lambda: None,
) -> RehostRuntimeTimeline:
    """Sample both private processes for the requested evidence window."""
    sampling_duration = (
        point.duration_seconds
        if sampling_duration_seconds is None
        else sampling_duration_seconds
    )
    if sampling_duration < point.duration_seconds:
        raise ValueError("runtime sampling cannot end before the scheduled load")
    sample_count = ceil(
        sampling_duration / RUNTIME_SAMPLE_INTERVAL_SECONDS
    ) + 1
    samples = []
    for sample_index in range(sample_count):
        api_response = api_client.get("/api/v1/experiments/runtime-metrics")
        api_response.raise_for_status()
        downstream_response = downstream_client.get(
            "/experiments/runtime-metrics"
        )
        downstream_response.raise_for_status()
        samples.append(
            DeploymentRuntimeSample(
                api=RuntimeMetricsSnapshot.model_validate(api_response.json()),
                downstream=RuntimeMetricsSnapshot.model_validate(
                    downstream_response.json()
                ),
            )
        )
        if sample_index == 0:
            on_ready()
        if sample_index < sample_count - 1:
            sleeper(RUNTIME_SAMPLE_INTERVAL_SECONDS)
    return RehostRuntimeTimeline(
        test_run_id=point.test_run_id,
        sampling_duration_seconds=sampling_duration,
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
        action_parser.add_argument("--sampling-duration-seconds", type=int)
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
                timeout=settings.downstream_timeout_seconds,
            ) as api_client,
            httpx.Client(
                base_url=settings.downstream_url,
                timeout=settings.downstream_timeout_seconds,
            ) as downstream_client,
        ):
            timeline = sample_rehost_runtime_timeline(
                point,
                api_client=api_client,
                downstream_client=downstream_client,
                sampling_duration_seconds=(
                    arguments.sampling_duration_seconds
                ),
                on_ready=lambda: print(
                    "TRACKRELAY_RUNTIME_SAMPLING_READY",
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
