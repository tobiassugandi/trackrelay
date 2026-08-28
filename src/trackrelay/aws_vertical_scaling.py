"""Prepare the guarded Stage 9.3 vertical-scaling experiment."""

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_rehost import (
    IMAGE_DIGEST_PATTERN,
    RDS_ENGINE_VERSION_PATTERN,
    AwsRehostError,
    ProcessRunner,
    require_applied_clean_revision,
    run_process,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    file_sha256,
    session_from_arguments,
    write_manifest,
)
from trackrelay.experiments.vertical_scaling import (
    DEFAULT_CAPACITY_SELECTION_PATH,
    DEFAULT_EXPERIMENT_CONTROLS_PATH,
    ECONOMICAL_BASELINE_INSTANCE_TYPE,
    InfrastructureScalingCapacitySelection,
    InfrastructureScalingControls,
    load_capacity_selection,
    load_experiment_controls,
)


class VerticalScalingExperimentDefinition(BaseModel):
    """Self-contained identity and controls frozen before load begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-synchronous-vertical-scaling-v1"] = (
        "aws-synchronous-vertical-scaling-v1"
    )
    prepared_at: AwareDatetime
    git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    application_image_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    aws_region: Literal["ap-southeast-3"]
    starting_instance_type: Literal["t4g.small"] = "t4g.small"
    rds_engine_version: str = Field(pattern=r"^17\.[0-9]+$")
    controls_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capacity_selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controls: InfrastructureScalingControls
    capacity_selection: InfrastructureScalingCapacitySelection

    @model_validator(mode="after")
    def require_one_consistent_experiment(self) -> "VerticalScalingExperimentDefinition":
        if self.controls.tier_order != (
            self.capacity_selection.economical_baseline.instance_type,
            self.capacity_selection.workload_fit.instance_type,
            self.capacity_selection.vertical_scale.instance_type,
        ):
            raise ValueError("capacity selection differs from the frozen tier order")
        if self.capacity_selection.aws_region != self.aws_region:
            raise ValueError("capacity selection differs from the session region")
        return self


class LoadExecutionWindow(BaseModel):
    """Exact driver-side boundaries used to align one run's AWS metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def require_positive_elapsed_time(self) -> "LoadExecutionWindow":
        if self.ended_at <= self.started_at:
            raise ValueError("load execution window must have positive duration")
        return self


@dataclass(frozen=True)
class TimedLoadResult[T]:
    """A load executor's result paired with its exact UTC boundaries."""

    value: T
    window: LoadExecutionWindow


def execute_with_load_window[T](
    test_run_id: UUID,
    operation: Callable[[], T],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TimedLoadResult[T]:
    """Measure only the operation itself, excluding setup and evidence polling."""
    started_at = now()
    value = operation()
    ended_at = now()
    return TimedLoadResult(
        value=value,
        window=LoadExecutionWindow(
            test_run_id=test_run_id,
            started_at=started_at,
            ended_at=ended_at,
        ),
    )


def _validated_image_digest(
    manifest: dict[str, object],
    *,
    revision: str,
) -> str:
    image = manifest.get("image")
    if not isinstance(image, dict):
        raise AwsRehostError("the scaling experiment needs a published image")
    digest = image.get("digest")
    tag = image.get("tag")
    if not isinstance(digest, str) or IMAGE_DIGEST_PATTERN.fullmatch(digest) is None:
        raise AwsRehostError("the scaling experiment image digest is invalid")
    if tag != f"git-{revision[:12]}":
        raise AwsRehostError("the scaling experiment image differs from the revision")
    return digest


def _validated_rds_engine_version(
    manifest: dict[str, object],
    *,
    controls: InfrastructureScalingControls,
) -> str:
    rds = manifest.get("rds")
    if not isinstance(rds, dict):
        raise AwsRehostError("the scaling experiment needs an RDS deployment")
    engine_version = rds.get("engine_version")
    if (
        not isinstance(engine_version, str)
        or RDS_ENGINE_VERSION_PATTERN.fullmatch(engine_version) is None
    ):
        raise AwsRehostError("the scaling experiment RDS version is invalid")
    expected = {
        "allocated_storage_gib": controls.rds.allocated_storage_gib,
        "database_placement": "private-single-az-rds",
        "engine": "postgres",
        "instance_class": controls.rds.instance_class,
        "storage_type": "encrypted-gp3",
    }
    if any(rds.get(field) != value for field, value in expected.items()):
        raise AwsRehostError("the deployed RDS settings differ from the controls")
    if engine_version.partition(".")[0] != controls.rds.engine_major_version:
        raise AwsRehostError("the deployed RDS major version differs from the controls")
    return engine_version


def build_experiment_definition(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    revision: str,
    prepared_at: datetime,
    controls_path: Path = DEFAULT_EXPERIMENT_CONTROLS_PATH,
    capacity_selection_path: Path = DEFAULT_CAPACITY_SELECTION_PATH,
) -> VerticalScalingExperimentDefinition:
    """Bind committed controls to the actual image and RDS deployment."""
    if session.region != "ap-southeast-3":
        raise AwsRehostError("Stage 9.3 is frozen to Asia Pacific (Jakarta)")
    if session.rehost_instance_type != ECONOMICAL_BASELINE_INSTANCE_TYPE:
        raise AwsRehostError("Stage 9.3 must begin on the economical baseline")
    controls = load_experiment_controls(path=controls_path)
    capacity_selection = load_capacity_selection(path=capacity_selection_path)
    return VerticalScalingExperimentDefinition(
        prepared_at=prepared_at,
        git_revision=revision,
        application_image_digest=_validated_image_digest(
            manifest,
            revision=revision,
        ),
        aws_region=session.region,
        rds_engine_version=_validated_rds_engine_version(
            manifest,
            controls=controls,
        ),
        controls_sha256=file_sha256(controls_path),
        capacity_selection_sha256=file_sha256(capacity_selection_path),
        controls=controls,
        capacity_selection=capacity_selection,
    )


def prepare_vertical_scaling_experiment(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerticalScalingExperimentDefinition:
    """Freeze Stage 9.3 locally after RDS correctness has passed."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "rds_correctness_collected":
        raise AwsRehostError(
            "RDS correctness must pass before Stage 9.3 is prepared"
        )
    definition = build_experiment_definition(
        session,
        manifest=manifest,
        revision=revision,
        prepared_at=now(),
    )
    output_root = session.evidence_dir / "vertical-scaling"
    output_root.mkdir(parents=True, exist_ok=False)
    definition_path = output_root / "experiment-definition.json"
    definition_path.write_text(
        f"{definition.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    manifest.update(
        {
            "status": "vertical_scaling_ready",
            "vertical_scaling": {
                "completed_tiers": [],
                "current_tier": ECONOMICAL_BASELINE_INSTANCE_TYPE,
                "definition": "vertical-scaling/experiment-definition.json",
                "definition_sha256": file_sha256(definition_path),
                "prepared_at": definition.prepared_at.isoformat(),
            },
        }
    )
    write_manifest(session, manifest)
    return definition


def build_parser() -> ArgumentParser:
    """Build the Stage 9.3 controller without embedding credentials."""
    parser = ArgumentParser(description=__doc__)
    add_shared_arguments(parser)
    return parser


def prepare_from_arguments(arguments: Namespace) -> VerticalScalingExperimentDefinition:
    """Build the shared session object and freeze the experiment."""
    return prepare_vertical_scaling_experiment(session_from_arguments(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare Stage 9.3 without provisioning or sending traffic."""
    arguments = build_parser().parse_args(argv)
    try:
        definition = prepare_from_arguments(arguments)
    except (AwsRehostError, AwsSessionError) as error:
        raise SystemExit(f"AWS vertical-scaling preparation failed: {error}") from error
    print("prepared the frozen Stage 9.3 experiment definition")
    print(f"starting tier: {definition.starting_instance_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
