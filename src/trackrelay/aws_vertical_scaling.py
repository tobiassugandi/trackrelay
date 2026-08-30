"""Prepare the guarded Stage 9.3 vertical-scaling experiment."""

import json
from argparse import ArgumentParser, Namespace
from base64 import b64decode
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from shlex import quote
from time import sleep
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trackrelay.aws_cloudwatch import (
    CloudWatchRunEvidence,
    collect_cloudwatch_evidence,
)
from trackrelay.aws_rehost import (
    COMMAND_ID_PATTERN,
    IMAGE_DIGEST_PATTERN,
    INSTANCE_ID_PATTERN,
    RDS_ENGINE_VERSION_PATTERN,
    RDS_IDENTIFIER_PATTERN,
    AwsRehostError,
    ProcessRunner,
    aws_prefix,
    deployed_image_reference,
    invoke,
    require_applied_clean_revision,
    run_process,
    run_ssm_payload,
    terraform_output,
    wait_for_ssm_online,
)
from trackrelay.aws_rehost_workload import (
    BenchmarkDriverEnvironment,
    RemoteRuntimeSampler,
    capture_benchmark_driver_environment,
    cleanup_remote_runtime_sampling,
    collect_remote_runtime_sampling,
    execute_local_load,
    run_remote_action,
    start_remote_runtime_sampling,
    stop_remote_runtime_sampling,
    validate_runtime_timeline_covers_load,
)
from trackrelay.aws_session import (
    AwsSession,
    AwsSessionError,
    add_shared_arguments,
    file_sha256,
    session_from_arguments,
    write_command_log,
    write_manifest,
)
from trackrelay.experiments.baseline import (
    LegacyBaselineBenchmarkDefinition,
    LegacyBaselineRateResult,
    compact_rate_result,
    derive_legacy_baseline_summary,
)
from trackrelay.experiments.generator import write_input_manifest
from trackrelay.experiments.performance import (
    PerformanceExperimentConfiguration,
    RuntimeMetricsSamples,
    build_k6_command,
    derive_performance_result,
)
from trackrelay.experiments.rehost import (
    RESET_EVIDENCE_PREFIX,
    RehostExperimentResetEvidence,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
)
from trackrelay.experiments.vertical_scaling import (
    ALLOWED_INSTANCE_TYPES,
    DEFAULT_CAPACITY_SELECTION_PATH,
    DEFAULT_EXPERIMENT_CONTROLS_PATH,
    ECONOMICAL_BASELINE_INSTANCE_TYPE,
    InfrastructureScalingCapacitySelection,
    InfrastructureScalingControls,
    load_capacity_selection,
    load_experiment_controls,
)
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot

FROZEN_SOURCE_BENCHMARK_PATH = (
    Path(__file__).resolve().parents[2]
    / "results"
    / "legacy-baseline"
    / "benchmark-definition.json"
)
TierRole = Literal[
    "economical-baseline",
    "workload-fit-migration",
    "within-family-scale-up",
]
CloudWatchCollector = Callable[..., CloudWatchRunEvidence]
TimedLocalLoadValue = tuple[
    int,
    tuple[RuntimeMetricsSnapshot, ...],
    dict[str, object],
]
TimedLocalLoadExecutor = Callable[
    ...,
    "TimedLoadResult[TimedLocalLoadValue]",
]
RemoteResetter = Callable[..., RehostExperimentResetEvidence]
SsmWaiter = Callable[..., None]
DeploymentValidator = Callable[..., str]


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


class VerticalScalingTierDefinition(BaseModel):
    """Portable candidate inputs and driver context for one hardware treatment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    experiment_name: Literal["aws-synchronous-vertical-scaling-v1"] = (
        "aws-synchronous-vertical-scaling-v1"
    )
    experiment_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_type: str
    tier_role: TierRole
    started_at: AwareDatetime
    offered_rates_per_second: tuple[int, ...]
    rate_duration_seconds: Literal[180]
    runtime_sample_interval_seconds: Literal[5]
    benchmark_driver: BenchmarkDriverEnvironment

    @model_validator(mode="after")
    def require_the_matching_tier_role(self) -> "VerticalScalingTierDefinition":
        expected_roles = {
            "t4g.small": "economical-baseline",
            "c8g.large": "workload-fit-migration",
            "c8g.4xlarge": "within-family-scale-up",
        }
        if expected_roles.get(self.instance_type) != self.tier_role:
            raise ValueError("hardware tier and experimental role differ")
        if tuple(sorted(set(self.offered_rates_per_second))) != (
            self.offered_rates_per_second
        ):
            raise ValueError("tier rates must be unique and strictly increasing")
        return self


class VerticalScalingTierSummary(BaseModel):
    """Compact capacity result for one unchanged-application treatment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    benchmark_name: Literal["aws-synchronous-vertical-scaling-v1"] = (
        "aws-synchronous-vertical-scaling-v1"
    )
    instance_type: str
    tier_role: TierRole
    completed_at: AwareDatetime
    maximum_sustainable_rate_per_second: int | None
    first_failing_rate_per_second: int | None
    capacity_is_at_least_highest_tested_rate: bool
    rate_results: tuple[LegacyBaselineRateResult, ...]


class InfrastructureTransitionPlanEvidence(BaseModel):
    """Sanitized proof that one saved plan changes only the EC2 host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_instance_type: str
    target_instance_type: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_resource_address: Literal["aws_instance.rehost"] = (
        "aws_instance.rehost"
    )
    actions: tuple[Literal["update"], ...]
    changed_attributes: tuple[str, ...]

    @model_validator(mode="after")
    def require_one_in_place_update(self) -> "InfrastructureTransitionPlanEvidence":
        if self.actions != ("update",):
            raise ValueError("EC2 transition must be one in-place update")
        if "instance_type" not in self.changed_attributes:
            raise ValueError("EC2 transition must change the instance type")
        return self


class VerticalScalingTransitionEvidence(BaseModel):
    """Portable reset, plan, apply, and validation proof for one transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_instance_type: str
    target_instance_type: str
    reset: RehostExperimentResetEvidence
    plan: InfrastructureTransitionPlanEvidence
    applied_at: AwareDatetime
    validated_at: AwareDatetime
    observed_instance_type: str
    instance_identity_preserved: Literal[True] = True
    rds_identity_preserved: Literal[True] = True
    deployment_validation_command_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    @model_validator(mode="after")
    def require_the_observed_target(self) -> "VerticalScalingTransitionEvidence":
        if self.observed_instance_type != self.target_instance_type:
            raise ValueError("observed EC2 type differs from the transition target")
        if self.validated_at < self.applied_at:
            raise ValueError("transition validation cannot precede apply")
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


def execute_timed_local_load(
    command: Sequence[str],
    client: httpx.Client,
    sample_interval_seconds: float,
    summary_path: Path,
    test_run_id: UUID,
    now: Callable[[], datetime],
    *,
    on_load_ended: Callable[[], None] = lambda: None,
) -> TimedLoadResult[
    TimedLocalLoadValue
]:
    """Capture the k6 process boundaries inside the existing load executor."""
    boundaries: list[datetime] = []

    def record_load_ended() -> None:
        boundaries.append(now())
        on_load_ended()

    value = execute_local_load(
        command,
        client,
        sample_interval_seconds,
        summary_path,
        on_load_started=lambda: boundaries.append(now()),
        on_load_ended=record_load_ended,
    )
    if len(boundaries) != 2:
        raise AwsRehostError("k6 did not expose one complete load window")
    return TimedLoadResult(
        value=value,
        window=LoadExecutionWindow(
            test_run_id=test_run_id,
            started_at=boundaries[0],
            ended_at=boundaries[1],
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


def _load_prepared_definition(
    session: AwsSession,
    *,
    manifest: dict[str, object],
    revision: str,
    require_current_incomplete: bool = True,
) -> tuple[VerticalScalingExperimentDefinition, Path]:
    """Revalidate the immutable preparation artifact before a tier runs."""
    scaling = manifest.get("vertical_scaling")
    if not isinstance(scaling, dict):
        raise AwsRehostError("Stage 9.3 preparation metadata is missing")
    if scaling.get("current_tier") != session.rehost_instance_type:
        raise AwsRehostError("session command differs from the current hardware tier")
    completed_tiers = scaling.get("completed_tiers")
    if not isinstance(completed_tiers, list) or any(
        not isinstance(tier, str) for tier in completed_tiers
    ):
        raise AwsRehostError("completed Stage 9.3 tiers are invalid")
    if (
        require_current_incomplete
        and session.rehost_instance_type in completed_tiers
    ):
        raise AwsRehostError("the current hardware tier is already complete")
    expected_relative_path = "vertical-scaling/experiment-definition.json"
    if scaling.get("definition") != expected_relative_path:
        raise AwsRehostError("Stage 9.3 definition path is invalid")
    definition_path = session.evidence_dir / expected_relative_path
    if (
        not definition_path.is_file()
        or scaling.get("definition_sha256") != file_sha256(definition_path)
    ):
        raise AwsRehostError("Stage 9.3 definition changed after preparation")
    try:
        definition = VerticalScalingExperimentDefinition.model_validate_json(
            definition_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise AwsRehostError("Stage 9.3 definition is invalid") from error
    if definition.git_revision != revision:
        raise AwsRehostError("Stage 9.3 definition differs from the Git revision")
    if definition.application_image_digest != _validated_image_digest(
        manifest,
        revision=revision,
    ):
        raise AwsRehostError("Stage 9.3 definition differs from the deployed image")
    if definition.rds_engine_version != _validated_rds_engine_version(
        manifest,
        controls=definition.controls,
    ):
        raise AwsRehostError("Stage 9.3 definition differs from deployed RDS")
    if definition.controls_sha256 != file_sha256(
        DEFAULT_EXPERIMENT_CONTROLS_PATH
    ):
        raise AwsRehostError("Stage 9.3 controls changed after preparation")
    if definition.capacity_selection_sha256 != file_sha256(
        DEFAULT_CAPACITY_SELECTION_PATH
    ):
        raise AwsRehostError("Stage 9.3 hardware selection changed after preparation")
    return definition, definition_path


def _tier_role(instance_type: str) -> TierRole:
    roles: dict[str, TierRole] = {
        "t4g.small": "economical-baseline",
        "c8g.large": "workload-fit-migration",
        "c8g.4xlarge": "within-family-scale-up",
    }
    try:
        return roles[instance_type]
    except KeyError as error:
        raise AwsRehostError("current hardware tier is not approved") from error


def _write_model(
    model: BaseModel,
    path: Path,
    *,
    exclude_computed_fields: bool = False,
) -> None:
    path.write_text(
        f"{model.model_dump_json(indent=2, exclude_computed_fields=exclude_computed_fields)}\n",
        encoding="utf-8",
    )


def _source_benchmark() -> LegacyBaselineBenchmarkDefinition:
    try:
        return LegacyBaselineBenchmarkDefinition.model_validate_json(
            FROZEN_SOURCE_BENCHMARK_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise AwsRehostError("the frozen source workload is invalid") from error


def _derive_tier_summary(
    *,
    instance_type: str,
    rate_results: Sequence[LegacyBaselineRateResult],
    completed_at: datetime,
) -> VerticalScalingTierSummary:
    baseline = derive_legacy_baseline_summary(
        rate_results,
        completed_at=completed_at,
    )
    return VerticalScalingTierSummary(
        instance_type=instance_type,
        tier_role=_tier_role(instance_type),
        completed_at=baseline.completed_at,
        maximum_sustainable_rate_per_second=(
            baseline.maximum_sustainable_rate_per_second
        ),
        first_failing_rate_per_second=baseline.first_failing_rate_per_second,
        capacity_is_at_least_highest_tested_rate=(
            baseline.capacity_is_at_least_highest_tested_rate
        ),
        rate_results=baseline.rate_results,
    )


def run_current_vertical_scaling_tier(
    session: AwsSession,
    *,
    runner: ProcessRunner = run_process,
    load_executor: TimedLocalLoadExecutor = execute_timed_local_load,
    remote_action: Callable[..., RehostServerEvidence | None] = (
        run_remote_action
    ),
    runtime_starter: Callable[..., RemoteRuntimeSampler] = (
        start_remote_runtime_sampling
    ),
    runtime_collector: Callable[..., RehostRuntimeTimeline] = (
        collect_remote_runtime_sampling
    ),
    runtime_stopper: Callable[..., None] = stop_remote_runtime_sampling,
    runtime_cleaner: Callable[..., None] = cleanup_remote_runtime_sampling,
    cloudwatch_collector: CloudWatchCollector = collect_cloudwatch_evidence,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uuid_factory: Callable[[], UUID] = uuid4,
) -> VerticalScalingTierSummary:
    """Run the current EC2 tier through its first complete failing rate."""
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "vertical_scaling_ready":
        raise AwsRehostError("the current Stage 9.3 tier is not ready to run")
    definition, definition_path = _load_prepared_definition(
        session,
        manifest=manifest,
        revision=revision,
    )
    controls = definition.controls.workload
    source = _source_benchmark().configuration
    instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")
    rds_identifier = terraform_output(
        session,
        "rds_identifier",
        runner=runner,
    )
    if RDS_IDENTIFIER_PATTERN.fullmatch(rds_identifier) is None:
        raise AwsRehostError("Terraform returned an invalid RDS identifier")
    public_ip_text = terraform_output(
        session,
        "rehost_public_ip",
        runner=runner,
    )
    try:
        public_ip = IPv4Address(public_ip_text)
    except ValueError as error:
        raise AwsRehostError("Terraform returned an invalid public IPv4") from error

    tier_root = (
        session.evidence_dir
        / "vertical-scaling"
        / "tiers"
        / session.rehost_instance_type
    )
    tier_root.mkdir(parents=True, exist_ok=False)
    tier_definition = VerticalScalingTierDefinition(
        experiment_definition_sha256=file_sha256(definition_path),
        instance_type=session.rehost_instance_type,
        tier_role=_tier_role(session.rehost_instance_type),
        started_at=now(),
        offered_rates_per_second=controls.offered_rates_per_second,
        rate_duration_seconds=controls.tier_duration_seconds,
        runtime_sample_interval_seconds=(
            controls.runtime_sample_interval_seconds
        ),
        benchmark_driver=capture_benchmark_driver_environment(),
    )
    _write_model(tier_definition, tier_root / "tier-definition.json")

    rate_results = []
    api_url = f"http://{public_ip}:8000"
    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        for rate in controls.offered_rates_per_second:
            test_run_id = uuid_factory()
            point = RehostWorkloadPoint(
                test_run_id=test_run_id,
                request_rate_per_second=rate,
                duration_seconds=controls.tier_duration_seconds,
                random_seed=controls.random_seed,
                partner_id=controls.partner_id,
                start_at=source.start_at,
                post_load_settle_timeout_seconds=(
                    controls.post_load_settle_timeout_seconds
                ),
                post_load_stable_window_seconds=(
                    controls.post_load_stable_window_seconds
                ),
            )
            run_directory = tier_root / "rates" / str(rate) / str(test_run_id)
            run_directory.mkdir(parents=True)
            input_manifest_path = run_directory / "input-manifest.json"
            k6_summary_path = run_directory / "k6-summary.json"
            local_runtime_path = run_directory / "runtime-metrics-samples.json"
            deployment_runtime_path = (
                run_directory / "deployment-runtime-timeline.json"
            )
            server_evidence_path = run_directory / "server-evidence.json"
            load_window_path = run_directory / "load-window.json"
            cloudwatch_path = run_directory / "cloudwatch-metrics.json"
            performance_result_path = (
                run_directory / "performance-result.json"
            )
            rate_result_path = run_directory / "rate-result.json"
            write_input_manifest(point.manifest(), input_manifest_path)

            remote_action(
                session,
                instance_id=instance_id,
                action="prepare",
                point=point,
                runner=runner,
            )
            configuration = PerformanceExperimentConfiguration(
                scenario="healthy",
                request_rate_per_second=rate,
                duration_seconds=controls.tier_duration_seconds,
                random_seed=controls.random_seed,
                partner_id=controls.partner_id,
                start_at=source.start_at,
                resource_sample_interval_seconds=(
                    controls.runtime_sample_interval_seconds
                ),
                post_load_settle_timeout_seconds=(
                    controls.post_load_settle_timeout_seconds
                ),
                post_load_stable_window_seconds=(
                    controls.post_load_stable_window_seconds
                ),
                k6_image=controls.k6_image,
                trackrelay_api_url=api_url,
                trackrelay_api_url_for_container=api_url,
            )
            command = build_k6_command(
                configuration,
                manifest_path=input_manifest_path,
                run_directory=run_directory,
            )
            runtime_sampler = runtime_starter(
                session,
                instance_id=instance_id,
                point=point,
                runner=runner,
            )

            def stop_sampler_after_load(
                sampler: RemoteRuntimeSampler = runtime_sampler,
                active_point: RehostWorkloadPoint = point,
            ) -> None:
                runtime_stopper(
                    session,
                    instance_id=instance_id,
                    sampler=sampler,
                    point=active_point,
                    runner=runner,
                )

            try:
                timed_load = load_executor(
                    command,
                    client,
                    controls.runtime_sample_interval_seconds,
                    k6_summary_path,
                    test_run_id,
                    now,
                    on_load_ended=stop_sampler_after_load,
                )
                k6_exit_code, local_samples, k6_summary = timed_load.value
                _write_model(timed_load.window, load_window_path)
                _write_model(
                    RuntimeMetricsSamples(
                        test_run_id=test_run_id,
                        samples=local_samples,
                    ),
                    local_runtime_path,
                )
                if not k6_summary_path.exists():
                    k6_summary_path.write_text(
                        f"{json.dumps(k6_summary, indent=2)}\n",
                        encoding="utf-8",
                    )
            except BaseException as load_error:
                try:
                    runtime_cleaner(
                        session,
                        instance_id=instance_id,
                        point=point,
                        runner=runner,
                    )
                except (AwsRehostError, OSError) as cleanup_error:
                    load_error.add_note(
                        "detached runtime sampler cleanup also failed: "
                        f"{type(cleanup_error).__name__}"
                    )
                raise
            runtime_timeline = runtime_collector(
                session,
                instance_id=instance_id,
                sampler=runtime_sampler,
                point=point,
                runner=runner,
            )
            validate_runtime_timeline_covers_load(
                runtime_timeline,
                test_run_id=test_run_id,
                load_started_at=timed_load.window.started_at,
                load_ended_at=timed_load.window.ended_at,
            )
            _write_model(runtime_timeline, deployment_runtime_path)

            server_evidence = remote_action(
                session,
                instance_id=instance_id,
                action="collect",
                point=point,
                runner=runner,
            )
            if server_evidence is None:
                raise AwsRehostError("SSM collection returned no evidence")
            _write_model(server_evidence, server_evidence_path)
            cloudwatch = cloudwatch_collector(
                session,
                instance_id=instance_id,
                rds_identifier=rds_identifier,
                test_run_id=test_run_id,
                load_started_at=timed_load.window.started_at,
                load_ended_at=timed_load.window.ended_at,
                runner=runner,
            )
            _write_model(cloudwatch, cloudwatch_path)

            performance_result = derive_performance_result(
                configuration,
                test_run_id=test_run_id,
                k6_exit_code=k6_exit_code,
                k6_summary=k6_summary,
                resource_samples=local_samples,
                reconciliation=server_evidence.reconciliation,
            )
            portable_configuration = configuration.model_copy(
                update={
                    "trackrelay_api_url": "http://stage-9-3-api.invalid",
                    "trackrelay_api_url_for_container": (
                        "http://stage-9-3-api.invalid"
                    ),
                    "downstream_url": "http://private-downstream.invalid",
                }
            )
            portable_performance_result = performance_result.model_copy(
                update={"configuration": portable_configuration}
            )
            _write_model(
                portable_performance_result,
                performance_result_path,
                exclude_computed_fields=True,
            )
            compact_result = compact_rate_result(
                performance_result,
                raw_evidence_directory=run_directory.relative_to(
                    session.evidence_dir
                ),
            )
            _write_model(compact_result, rate_result_path)
            rate_results.append(compact_result)
            if not compact_result.complete_experiment_passed:
                break

    summary = _derive_tier_summary(
        instance_type=session.rehost_instance_type,
        rate_results=rate_results,
        completed_at=now(),
    )
    summary_path = tier_root / "summary.json"
    _write_model(summary, summary_path)
    scaling = manifest["vertical_scaling"]
    completed_tiers = [*scaling["completed_tiers"], session.rehost_instance_type]
    scaling.update(
        {
            "completed_tiers": completed_tiers,
            "latest_tier_summary": str(
                summary_path.relative_to(session.evidence_dir)
            ),
        }
    )
    manifest["status"] = "vertical_scaling_tier_collected"
    write_manifest(session, manifest)
    return summary


def build_remote_reset_payload() -> dict[str, list[str]]:
    """Build the private synthetic-state reset command."""
    command = (
        "docker compose --project-name trackrelay-rehost "
        "--env-file /opt/trackrelay/.env "
        "--file /opt/trackrelay/compose.yaml run --rm --no-deps api "
        "python -m trackrelay.experiments.rehost reset"
    )
    payload = {
        "commands": [f"set -euo pipefail\n{command}"],
        "executionTimeout": ["300"],
    }
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM reset payload exceeds the safety limit")
    return payload


def run_remote_experiment_reset(
    session: AwsSession,
    *,
    instance_id: str,
    runner: ProcessRunner,
) -> RehostExperimentResetEvidence:
    """Clear and read back compact treatment-reset evidence through SSM."""
    command_id = run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_remote_reset_payload(),
        comment="TrackRelay Stage 9.3 treatment reset",
        runner=runner,
    )
    output = invoke(
        runner,
        (
            *aws_prefix(session),
            "ssm",
            "get-command-invocation",
            "--command-id",
            command_id,
            "--instance-id",
            instance_id,
            "--query",
            "StandardOutputContent",
            "--output",
            "text",
        ),
        action="SSM treatment-reset evidence collection",
    ).stdout
    evidence_lines = [
        line.removeprefix(RESET_EVIDENCE_PREFIX)
        for line in output.splitlines()
        if line.startswith(RESET_EVIDENCE_PREFIX)
    ]
    if len(evidence_lines) != 1:
        raise AwsRehostError("SSM returned ambiguous treatment-reset evidence")
    try:
        decoded = b64decode(evidence_lines[0], validate=True).decode("utf-8")
        return RehostExperimentResetEvidence.model_validate_json(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        raise AwsRehostError("SSM returned invalid treatment-reset evidence") from error


def validate_transition_plan(
    plan_json: str,
    *,
    source_instance_type: str,
    target_instance_type: str,
    plan_sha256: str,
) -> InfrastructureTransitionPlanEvidence:
    """Reject any saved plan that changes more than the one EC2 host."""
    try:
        document = json.loads(plan_json)
        resource_changes = document["resource_changes"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise AwsRehostError("Terraform returned an invalid transition plan") from error
    if not isinstance(resource_changes, list):
        raise AwsRehostError("Terraform transition changes must be a list")
    meaningful_changes = []
    for resource in resource_changes:
        try:
            actions = resource["change"]["actions"]
        except (KeyError, TypeError) as error:
            raise AwsRehostError("Terraform transition change is invalid") from error
        if actions != ["no-op"]:
            meaningful_changes.append(resource)
    if len(meaningful_changes) != 1:
        raise AwsRehostError("transition plan must change exactly one resource")
    resource = meaningful_changes[0]
    try:
        address = resource["address"]
        change = resource["change"]
        actions = tuple(change["actions"])
        before = change["before"]
        after = change["after"]
    except (KeyError, TypeError) as error:
        raise AwsRehostError("Terraform EC2 transition is incomplete") from error
    if address != "aws_instance.rehost" or actions != ("update",):
        raise AwsRehostError("transition plan is not an in-place EC2 update")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise AwsRehostError("Terraform EC2 values are invalid")
    if before.get("instance_type") != source_instance_type:
        raise AwsRehostError("transition plan source type differs from the session")
    if after.get("instance_type") != target_instance_type:
        raise AwsRehostError("transition plan target type differs from approval")
    changed_attributes = tuple(
        sorted(
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        )
    )
    permitted_changes = {
        "instance_type",
        "credit_specification",
        "cpu_options",
        "ebs_optimized",
    }
    unexpected_changes = set(changed_attributes) - permitted_changes
    if unexpected_changes:
        raise AwsRehostError(
            "transition plan changes unexpected EC2 attributes: "
            f"{sorted(unexpected_changes)}"
        )
    try:
        return InfrastructureTransitionPlanEvidence(
            source_instance_type=source_instance_type,
            target_instance_type=target_instance_type,
            plan_sha256=plan_sha256,
            actions=actions,
            changed_attributes=changed_attributes,
        )
    except ValueError as error:
        raise AwsRehostError("Terraform transition plan is invalid") from error


def build_transition_validation_payload(
    *,
    image_reference: str,
) -> dict[str, list[str]]:
    """Require the unchanged image and healthy RDS-backed API after restart."""
    if "@sha256:" not in image_reference:
        raise AwsRehostError("transition validation image is not digest pinned")
    expected_image = quote(image_reference)
    command = "\n".join(
        (
            "set -euo pipefail",
            (
                "actual_image=\"$(docker inspect --format "
                "'{{.Config.Image}}' trackrelay-rehost-api-1)\""
            ),
            f"test \"${{actual_image}}\" = {expected_image}",
            (
                "grep --quiet "
                "'^TRACKRELAY_DATABASE_URL=.*sslmode=require$' "
                "/opt/trackrelay/.env"
            ),
            (
                "test \"$(curl --silent --fail --max-time 5 "
                "http://127.0.0.1:8000/health/ready)\" "
                "= '{\"status\":\"ready\"}'"
            ),
        )
    )
    payload = {"commands": [command], "executionTimeout": ["120"]}
    if len(json.dumps(payload).encode("utf-8")) > 20_000:
        raise AwsRehostError("SSM transition validation exceeds the safety limit")
    return payload


def validate_transition_deployment(
    session: AwsSession,
    *,
    instance_id: str,
    image_reference: str,
    runner: ProcessRunner,
) -> str:
    """Verify containers, immutable image, and RDS readiness after resize."""
    return run_ssm_payload(
        session,
        instance_id=instance_id,
        payload=build_transition_validation_payload(
            image_reference=image_reference
        ),
        comment="TrackRelay Stage 9.3 transition validation",
        runner=runner,
    )


def transition_to_next_vertical_scaling_tier(
    session: AwsSession,
    *,
    target_instance_type: str,
    approved_session_id: str,
    approved_target_instance_type: str,
    runner: ProcessRunner = run_process,
    resetter: RemoteResetter = run_remote_experiment_reset,
    ssm_waiter: SsmWaiter = wait_for_ssm_online,
    deployment_validator: DeploymentValidator = validate_transition_deployment,
    sleeper: Callable[[float], None] = sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerticalScalingTransitionEvidence:
    """Reset state and apply only the saved, single-host next-tier plan."""
    if approved_session_id != session.session_id:
        raise AwsRehostError("approved transition session ID does not match")
    if approved_target_instance_type != target_instance_type:
        raise AwsRehostError("approved transition target does not match")
    manifest, revision = require_applied_clean_revision(session, runner=runner)
    if manifest.get("status") != "vertical_scaling_tier_collected":
        raise AwsRehostError("the current Stage 9.3 tier is not complete")
    definition, _ = _load_prepared_definition(
        session,
        manifest=manifest,
        revision=revision,
        require_current_incomplete=False,
    )
    tiers = definition.controls.tier_order
    try:
        source_index = tiers.index(session.rehost_instance_type)
    except ValueError as error:
        raise AwsRehostError("current tier is outside the experiment order") from error
    if source_index + 1 >= len(tiers):
        raise AwsRehostError("the final hardware tier has no next transition")
    expected_target = tiers[source_index + 1]
    if target_instance_type != expected_target:
        raise AwsRehostError("transition target must be the next frozen tier")
    scaling = manifest["vertical_scaling"]
    if scaling["completed_tiers"] != list(tiers[: source_index + 1]):
        raise AwsRehostError("completed tiers are not the expected ordered prefix")

    source_instance_id = terraform_output(
        session,
        "rehost_instance_id",
        runner=runner,
    )
    if INSTANCE_ID_PATTERN.fullmatch(source_instance_id) is None:
        raise AwsRehostError("Terraform returned an invalid EC2 instance ID")
    source_rds_identifier = terraform_output(
        session,
        "rds_identifier",
        runner=runner,
    )
    if RDS_IDENTIFIER_PATTERN.fullmatch(source_rds_identifier) is None:
        raise AwsRehostError("Terraform returned an invalid RDS identifier")

    transition_name = f"{session.rehost_instance_type}-to-{target_instance_type}"
    transition_root = (
        session.evidence_dir
        / "vertical-scaling"
        / "transitions"
        / transition_name
    )
    transition_root.mkdir(parents=True, exist_ok=False)
    reset_evidence = resetter(
        session,
        instance_id=source_instance_id,
        runner=runner,
    )
    _write_model(reset_evidence, transition_root / "reset-evidence.json")

    target_session = replace(
        session,
        rehost_instance_type=target_instance_type,
    )
    plan_path = transition_root / "terraform-transition.tfplan"
    plan_result = invoke(
        runner,
        target_session.terraform_command(
            "plan",
            "-input=false",
            f"-out={plan_path.resolve()}",
            *target_session.terraform_variables(),
        ),
        action="Terraform EC2 transition plan",
    )
    write_command_log(transition_root / "terraform-plan.log", plan_result)
    if not plan_path.is_file():
        raise AwsRehostError("Terraform did not save the transition plan")
    plan_digest = file_sha256(plan_path)
    plan_json = invoke(
        runner,
        target_session.terraform_command(
            "show",
            "-json",
            plan_path.resolve().as_posix(),
        ),
        action="Terraform EC2 transition inspection",
    ).stdout
    plan_evidence = validate_transition_plan(
        plan_json,
        source_instance_type=session.rehost_instance_type,
        target_instance_type=target_instance_type,
        plan_sha256=plan_digest,
    )
    _write_model(plan_evidence, transition_root / "plan-evidence.json")
    manifest["status"] = "vertical_scaling_transition_planned"
    scaling["pending_transition"] = {
        "plan_sha256": plan_digest,
        "source_instance_type": session.rehost_instance_type,
        "target_instance_type": target_instance_type,
    }
    write_manifest(session, manifest)

    apply_result = invoke(
        runner,
        target_session.terraform_command(
            "apply",
            "-input=false",
            plan_path.resolve().as_posix(),
        ),
        action="Terraform EC2 transition apply",
    )
    write_command_log(transition_root / "terraform-apply.log", apply_result)
    applied_at = now()
    manifest["rehost_instance_type"] = target_instance_type
    manifest["status"] = "vertical_scaling_transition_applied_pending_validation"
    scaling["current_tier"] = target_instance_type
    scaling["pending_transition"]["applied_at"] = applied_at.isoformat()
    write_manifest(target_session, manifest)

    target_instance_id = terraform_output(
        target_session,
        "rehost_instance_id",
        runner=runner,
    )
    if target_instance_id != source_instance_id:
        raise AwsRehostError("EC2 transition replaced the host identity")
    target_rds_identifier = terraform_output(
        target_session,
        "rds_identifier",
        runner=runner,
    )
    if target_rds_identifier != source_rds_identifier:
        raise AwsRehostError("EC2 transition changed the RDS identity")
    observed_instance_type = invoke(
        runner,
        (
            *aws_prefix(target_session),
            "ec2",
            "describe-instances",
            "--instance-ids",
            target_instance_id,
            "--query",
            "Reservations[0].Instances[0].InstanceType",
            "--output",
            "text",
        ),
        action="EC2 transition type verification",
    ).stdout.strip()
    if observed_instance_type != target_instance_type:
        raise AwsRehostError("EC2 reports a different transition target")
    ssm_waiter(
        target_session,
        target_instance_id,
        runner=runner,
        sleeper=sleeper,
    )
    image_reference = deployed_image_reference(
        target_session,
        manifest=manifest,
        revision=revision,
        runner=runner,
    )
    validation_command_id = deployment_validator(
        target_session,
        instance_id=target_instance_id,
        image_reference=image_reference,
        runner=runner,
    )
    if COMMAND_ID_PATTERN.fullmatch(validation_command_id) is None:
        raise AwsRehostError("transition validation returned an invalid command ID")
    validated_at = now()
    evidence = VerticalScalingTransitionEvidence(
        source_instance_type=session.rehost_instance_type,
        target_instance_type=target_instance_type,
        reset=reset_evidence,
        plan=plan_evidence,
        applied_at=applied_at,
        validated_at=validated_at,
        observed_instance_type=observed_instance_type,
        deployment_validation_command_id=validation_command_id,
    )
    evidence_path = transition_root / "transition-evidence.json"
    _write_model(evidence, evidence_path)
    transitions = scaling.setdefault("transitions", [])
    transitions.append(
        {
            "evidence": str(evidence_path.relative_to(session.evidence_dir)),
            "source_instance_type": session.rehost_instance_type,
            "target_instance_type": target_instance_type,
            "validated_at": validated_at.isoformat(),
        }
    )
    scaling.pop("pending_transition")
    manifest["status"] = "vertical_scaling_ready"
    write_manifest(target_session, manifest)
    return evidence


def build_parser() -> ArgumentParser:
    """Build the Stage 9.3 controller without embedding credentials."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_shared_arguments(subparsers.add_parser("prepare"))
    add_shared_arguments(subparsers.add_parser("run-tier"))
    transition_parser = subparsers.add_parser("transition")
    add_shared_arguments(transition_parser)
    transition_parser.add_argument(
        "--target-instance-type",
        required=True,
        choices=ALLOWED_INSTANCE_TYPES,
    )
    transition_parser.add_argument("--approved-session-id", required=True)
    transition_parser.add_argument(
        "--approved-target-instance-type",
        required=True,
        choices=ALLOWED_INSTANCE_TYPES,
    )
    return parser


def prepare_from_arguments(arguments: Namespace) -> VerticalScalingExperimentDefinition:
    """Build the shared session object and freeze the experiment."""
    return prepare_vertical_scaling_experiment(session_from_arguments(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    """Prepare Stage 9.3 or run its current approved hardware tier."""
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            definition = prepare_from_arguments(arguments)
            print("prepared the frozen Stage 9.3 experiment definition")
            print(f"starting tier: {definition.starting_instance_type}")
        elif arguments.command == "run-tier":
            summary = run_current_vertical_scaling_tier(
                session_from_arguments(arguments)
            )
            print("collected the current Stage 9.3 hardware tier")
            print(f"instance type: {summary.instance_type}")
            print(
                "maximum sustainable rate: "
                f"{summary.maximum_sustainable_rate_per_second} events/s"
            )
        else:
            evidence = transition_to_next_vertical_scaling_tier(
                session_from_arguments(arguments),
                target_instance_type=arguments.target_instance_type,
                approved_session_id=arguments.approved_session_id,
                approved_target_instance_type=(
                    arguments.approved_target_instance_type
                ),
            )
            print("completed the guarded Stage 9.3 hardware transition")
            print(
                f"instance type: {evidence.source_instance_type} -> "
                f"{evidence.target_instance_type}"
            )
    except (AwsRehostError, AwsSessionError) as error:
        raise SystemExit(f"AWS vertical-scaling command failed: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
