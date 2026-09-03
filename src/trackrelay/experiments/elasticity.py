"""Freeze and prepare the replayable Stage 9.6/9.7 workload."""

from argparse import ArgumentParser
from enum import StrEnum
from itertools import pairwise
from json import dumps
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from trackrelay.domain import ShipmentStatus
from trackrelay.experiments.generator import (
    DEFAULT_START_AT,
    GeneratorConfiguration,
    InputManifest,
    generate_input_manifest,
)

PositiveInteger = Annotated[int, Field(gt=0)]


class ElasticityTreatment(StrEnum):
    """The only causal treatments in the final comparison."""

    FIXED = "fixed"
    ELASTIC = "elastic"


class ElasticityWorkloadStep(BaseModel):
    """One constant-arrival-rate plateau in the replayable waveform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    offered_rate_per_second: PositiveInteger
    duration_seconds: PositiveInteger

    @computed_field
    @property
    def expected_request_count(self) -> int:
        return self.offered_rate_per_second * self.duration_seconds


DEFAULT_ELASTICITY_STEPS = (
    ElasticityWorkloadStep(
        name="baseline",
        offered_rate_per_second=1,
        duration_seconds=60,
    ),
    ElasticityWorkloadStep(
        name="rise-5",
        offered_rate_per_second=5,
        duration_seconds=120,
    ),
    ElasticityWorkloadStep(
        name="rise-10",
        offered_rate_per_second=10,
        duration_seconds=120,
    ),
    ElasticityWorkloadStep(
        name="peak-25",
        offered_rate_per_second=25,
        duration_seconds=300,
    ),
    ElasticityWorkloadStep(
        name="fall-10",
        offered_rate_per_second=10,
        duration_seconds=60,
    ),
    ElasticityWorkloadStep(
        name="fall-5",
        offered_rate_per_second=5,
        duration_seconds=60,
    ),
    ElasticityWorkloadStep(
        name="recovery",
        offered_rate_per_second=1,
        duration_seconds=420,
    ),
)


class ElasticityWorkloadDefinition(BaseModel):
    """Candidate waveform shared byte-for-byte by fixed and elastic runs.

    The rates are a qualification candidate informed by cloud-session-3 drain
    behavior. They become the final comparison only after the fixed run proves
    that the peak exceeds one-worker delivery capacity while non-worker tiers
    retain headroom.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-elasticity-candidate-v2", "aws-elasticity-candidate-v3"] = (
        "aws-elasticity-candidate-v3"
    )
    steps: tuple[ElasticityWorkloadStep, ...] = DEFAULT_ELASTICITY_STEPS
    random_seed: int = 20260901
    partner_id: str = "elasticity-alpha"
    metric_period_seconds: Literal[60] = 60
    metric_start_alignment_tolerance_seconds: Literal[1.0] = 1.0
    ingestion_p95_limit_ms: Literal[500] = 500
    ingestion_error_limit_percent: Literal[1.0] = 1.0
    maximum_non_worker_utilization_percent: Literal[70.0] = 70.0
    maximum_database_pool_utilization_percent: Literal[70.0] = 70.0
    maximum_rds_cpu_utilization_percent: Literal[70.0] = 70.0
    maximum_rds_connections: Literal[50] = 50
    minimum_rds_freeable_memory_bytes: Literal[134217728] = 134217728
    maximum_rds_read_latency_seconds: Literal[0.02] = 0.02
    maximum_rds_write_latency_seconds: Literal[0.02] = 0.02
    minimum_worker_count: Literal[1] = 1
    k6_image: str = "grafana/k6:2.1.0"
    trackrelay_api_url_for_container: str = "http://host.docker.internal:8000"

    @computed_field
    @property
    def expected_request_count(self) -> int:
        return sum(step.expected_request_count for step in self.steps)

    @computed_field
    @property
    def duration_seconds(self) -> int:
        return sum(step.duration_seconds for step in self.steps)

    @computed_field
    @property
    def peak_rate_per_second(self) -> int:
        return max(step.offered_rate_per_second for step in self.steps)

    @computed_field
    @property
    def event_offsets(self) -> tuple[int, ...]:
        offsets = []
        next_offset = 0
        for step in self.steps:
            offsets.append(next_offset)
            next_offset += step.expected_request_count
        return tuple(offsets)

    @model_validator(mode="after")
    def require_one_aligned_rise_and_fall(self) -> "ElasticityWorkloadDefinition":
        if len(self.steps) < 5:
            raise ValueError("elasticity workload requires at least five steps")
        names = tuple(step.name for step in self.steps)
        if len(set(names)) != len(names):
            raise ValueError("elasticity workload step names must be unique")
        if any(
            step.duration_seconds % self.metric_period_seconds for step in self.steps
        ):
            raise ValueError(
                "every elasticity step must align to the 60-second metric period"
            )
        rates = tuple(step.offered_rate_per_second for step in self.steps)
        peak = max(rates)
        if rates.count(peak) != 1:
            raise ValueError("elasticity workload requires one unique peak")
        peak_index = rates.index(peak)
        if peak_index == 0 or peak_index == len(rates) - 1:
            raise ValueError("elasticity peak must have rising and falling steps")
        if any(
            later <= earlier for earlier, later in pairwise(rates[: peak_index + 1])
        ):
            raise ValueError("elasticity rates must rise strictly to the peak")
        if any(later >= earlier for earlier, later in pairwise(rates[peak_index:])):
            raise ValueError("elasticity rates must fall strictly after the peak")
        if rates[0] != rates[-1]:
            raise ValueError("elasticity workload must return to its starting rate")
        minimum_recovery_periods = (
            7 if self.name == "aws-elasticity-candidate-v3" else 5
        )
        if (
            self.steps[-1].duration_seconds
            < minimum_recovery_periods * self.metric_period_seconds
        ):
            raise ValueError(
                "elasticity recovery is shorter than the candidate minimum"
            )
        return self


ELASTICITY_WORKLOAD_DEFINITION = ElasticityWorkloadDefinition()


def build_elasticity_manifest(
    treatment: ElasticityTreatment,
    *,
    definition: ElasticityWorkloadDefinition = ELASTICITY_WORKLOAD_DEFINITION,
    test_run_id: UUID | None = None,
) -> InputManifest:
    """Build one CREATED event per scheduled request with unique identities."""
    resolved_test_run_id = test_run_id or uuid4()
    generated = generate_input_manifest(
        seed=definition.random_seed,
        configuration=GeneratorConfiguration(
            partner_id=definition.partner_id,
            shipment_count=definition.expected_request_count,
            start_at=DEFAULT_START_AT,
        ),
        test_run_id=resolved_test_run_id,
    )
    created_events = tuple(
        event.model_copy(update={"sequence_number": sequence_number})
        for sequence_number, event in enumerate(
            (
                event
                for event in generated.expected_events
                if event.expected_status is ShipmentStatus.CREATED
            ),
            start=1,
        )
    )
    scenario_name = (
        "elasticity-fixed-control"
        if treatment is ElasticityTreatment.FIXED
        else "elasticity-elastic-treatment"
    )
    return InputManifest(
        test_run_id=resolved_test_run_id,
        scenario_name=scenario_name,
        seed=definition.random_seed,
        configuration=generated.configuration,
        events_generated=len(created_events),
        expected_unique_events=len(created_events),
        expected_events=created_events,
        expected_final_shipments={
            event.tracking_number: ShipmentStatus.CREATED for event in created_events
        },
    )


def build_elasticity_k6_command(
    definition: ElasticityWorkloadDefinition,
    *,
    manifest_path: Path,
    definition_path: Path,
    result_directory: Path,
    api_url_for_container: str | None = None,
) -> tuple[str, ...]:
    """Build the pinned, shell-free k6 invocation for the stepped waveform."""
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
            f"{api_url_for_container or definition.trackrelay_api_url_for_container}"
        ),
        "--env",
        "K6_MANIFEST_PATH=/input-manifest.json",
        "--env",
        "K6_WORKLOAD_PATH=/workload-definition.json",
        "--env",
        "K6_SUMMARY_PATH=/results/k6-summary.json",
        "--volume",
        f"{repository_root / 'load'}:/scripts:ro",
        "--volume",
        f"{manifest_path.resolve()}:/input-manifest.json:ro",
        "--volume",
        f"{definition_path.resolve()}:/workload-definition.json:ro",
        "--volume",
        f"{result_directory.resolve()}:/results",
        definition.k6_image,
        "run",
        "/scripts/elasticity-steps.js",
    )


def prepare_elasticity_workload(
    treatment: ElasticityTreatment,
    *,
    output_directory: Path,
    definition: ElasticityWorkloadDefinition = ELASTICITY_WORKLOAD_DEFINITION,
    test_run_id: UUID | None = None,
) -> tuple[Path, Path]:
    """Write the exact definition and manifest consumed by the load driver."""
    output_directory.mkdir(parents=True, exist_ok=False)
    manifest = build_elasticity_manifest(
        treatment,
        definition=definition,
        test_run_id=test_run_id,
    )
    definition_path = output_directory / "workload-definition.json"
    manifest_path = output_directory / "input-manifest.json"
    definition_path.write_text(
        definition.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (output_directory / "k6-command.json").write_text(
        dumps(
            build_elasticity_k6_command(
                definition,
                manifest_path=manifest_path,
                definition_path=definition_path,
                result_directory=output_directory,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return definition_path, manifest_path


def build_parser() -> ArgumentParser:
    """Build the local workload-preparation command."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--treatment",
        type=ElasticityTreatment,
        choices=tuple(ElasticityTreatment),
        required=True,
    )
    parser.add_argument("--test-run-id", type=UUID)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser


def main() -> None:
    """Prepare one treatment's deterministic workload without running it."""
    arguments = build_parser().parse_args()
    prepare_elasticity_workload(
        arguments.treatment,
        output_directory=arguments.output_directory,
        test_run_id=arguments.test_run_id,
    )
