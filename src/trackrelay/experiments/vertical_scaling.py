"""Machine-readable controls for synchronous infrastructure scaling."""

from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PositiveInteger = Annotated[int, Field(gt=0)]
PositivePrice = Annotated[Decimal, Field(gt=0)]
ECONOMICAL_BASELINE_INSTANCE_TYPE = "t4g.small"
WORKLOAD_FIT_INSTANCE_TYPE = "c8g.large"
VERTICAL_SCALE_INSTANCE_TYPE = "c8g.4xlarge"
ALLOWED_INSTANCE_TYPES = (
    ECONOMICAL_BASELINE_INSTANCE_TYPE,
    WORKLOAD_FIT_INSTANCE_TYPE,
    VERTICAL_SCALE_INSTANCE_TYPE,
)
DEFAULT_CAPACITY_SELECTION_PATH = (
    Path(__file__).resolve().parents[3]
    / "results"
    / "aws-vertical-scaling"
    / "ec2-capacity-selection.json"
)
DEFAULT_EXPERIMENT_CONTROLS_PATH = (
    Path(__file__).resolve().parents[3]
    / "results"
    / "aws-vertical-scaling"
    / "experiment-controls.json"
)
DEFAULT_SOURCE_BENCHMARK_PATH = (
    Path(__file__).resolve().parents[3]
    / "results"
    / "legacy-baseline"
    / "benchmark-definition.json"
)


class ApplicationControls(BaseModel):
    """Application identity and runtime settings held between hardware tiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_identity: Literal["same-committed-git-revision"]
    image_identity: Literal["same-ecr-sha256-digest"]
    architecture: Literal["synchronous"]
    api_process_count: Literal[1]
    api_access_log_enabled: Literal[False]
    database_pool_size: Literal[5]
    database_max_overflow: Literal[10]
    database_pool_pre_ping: Literal[True]
    downstream_timeout_seconds: Literal[5.0]


class RdsControls(BaseModel):
    """Fixed managed-database settings for every treatment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    placement: Literal["same-private-rds-instance"]
    engine_major_version: Literal["17"]
    instance_class: Literal["db.t4g.micro"]
    allocated_storage_gib: Literal[20]
    storage_type: Literal["gp3"]
    multi_az: Literal[False]
    parameter_group_identity: Literal["same-session-parameter-group"]


class DownstreamControls(BaseModel):
    """Healthy simulator configuration that may not grow with the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["healthy"]
    process_count: Literal[1]
    cpu_limit: Literal[1.0]
    memory_limit_mib: Literal[1024]
    access_log_enabled: Literal[False]


class WorkloadControls(BaseModel):
    """Frozen load and success criteria shared by all three runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_workload: Literal["legacy-local-baseline-v1"]
    source_benchmark_definition_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    shape: Literal["one-created-event-per-shipment"]
    offered_rates_per_second: tuple[PositiveInteger, ...]
    tier_duration_seconds: Literal[180]
    runtime_sample_interval_seconds: Literal[5]
    random_seed: Literal[20260806]
    partner_id: Literal["load-alpha"]
    k6_image: Literal["grafana/k6:2.1.0"]
    benchmark_driver_identity: Literal["same-host-and-container-image"]
    p95_latency_limit_ms: Literal[500.0]
    request_error_rate_limit: Literal[0.01]
    unaccounted_events_required: Literal[0]
    duplicate_business_effects_required: Literal[0]
    final_shipment_states_must_match: Literal[True]

    @model_validator(mode="after")
    def require_the_frozen_rate_ladder(self) -> "WorkloadControls":
        if self.offered_rates_per_second != (10, 25, 50, 100, 250, 500):
            raise ValueError("the Stage 9.3 offered-rate ladder must stay frozen")
        return self


class InfrastructureScalingControls(BaseModel):
    """Declare every constant and the sole permitted deployment change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-synchronous-infrastructure-scaling-controls-v1"]
    only_changed_deployment_input: Literal["ec2-instance-type"]
    tier_order: tuple[str, ...]
    experiment_state: Literal["fresh-identity-namespace-per-tier"]
    application: ApplicationControls
    rds: RdsControls
    downstream: DownstreamControls
    workload: WorkloadControls

    @model_validator(mode="after")
    def require_the_selected_tier_order(self) -> "InfrastructureScalingControls":
        if self.tier_order != ALLOWED_INSTANCE_TYPES:
            raise ValueError("the Stage 9.3 hardware-tier order must stay frozen")
        return self


class Ec2Capacity(BaseModel):
    """One regionally offered EC2 capacity and its observed catalog facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_type: str = Field(pattern=r"^[a-z][a-z0-9]*\.[a-z0-9]+$")
    architecture: Literal["arm64"] = "arm64"
    processor: str
    vcpu_count: PositiveInteger
    memory_mib: PositiveInteger
    network_performance: str
    burstable_performance: bool
    on_demand_linux_price_usd_per_hour: PositivePrice


class InfrastructureScalingCapacitySelection(BaseModel):
    """Freeze the three-role hardware ladder before cloud-session approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-synchronous-infrastructure-scaling-capacity-v1"] = (
        "aws-synchronous-infrastructure-scaling-capacity-v1"
    )
    aws_region: Literal["ap-southeast-3"] = "ap-southeast-3"
    pricing_location: Literal["Asia Pacific (Jakarta)"] = (
        "Asia Pacific (Jakarta)"
    )
    operating_system: Literal["Linux"] = "Linux"
    tenancy: Literal["Shared"] = "Shared"
    purchase_option: Literal["OnDemand"] = "OnDemand"
    availability_source: Literal["ec2:DescribeInstanceTypeOfferings"] = (
        "ec2:DescribeInstanceTypeOfferings"
    )
    pricing_source: Literal["pricing:GetProducts"] = "pricing:GetProducts"
    availability_checked_at: AwareDatetime
    price_catalog_publication_at: AwareDatetime
    price_catalog_version: str = Field(pattern=r"^[0-9]{14}$")
    economical_baseline: Ec2Capacity
    workload_fit: Ec2Capacity
    vertical_scale: Ec2Capacity

    @model_validator(mode="after")
    def require_the_intended_hardware_progression(
        self,
    ) -> "InfrastructureScalingCapacitySelection":
        tiers = (
            self.economical_baseline,
            self.workload_fit,
            self.vertical_scale,
        )
        if len({tier.instance_type for tier in tiers}) != len(tiers):
            raise ValueError("every hardware tier must use a distinct instance type")
        if not self.economical_baseline.burstable_performance:
            raise ValueError("the economical baseline must retain burstable behavior")
        if (
            self.workload_fit.burstable_performance
            or self.vertical_scale.burstable_performance
        ):
            raise ValueError("both compute-optimized tiers must be non-burstable")
        workload_family = self.workload_fit.instance_type.partition(".")[0]
        scale_family = self.vertical_scale.instance_type.partition(".")[0]
        if workload_family != scale_family:
            raise ValueError("the two compute tiers must use the same EC2 family")
        if self.workload_fit.processor != self.vertical_scale.processor:
            raise ValueError("the two compute tiers must use the same processor")
        if self.vertical_scale.vcpu_count <= self.workload_fit.vcpu_count:
            raise ValueError("vertical scale must have more vCPUs than workload fit")
        if self.vertical_scale.memory_mib <= self.workload_fit.memory_mib:
            raise ValueError("vertical scale must have more memory than workload fit")
        prices = tuple(
            tier.on_demand_linux_price_usd_per_hour for tier in tiers
        )
        if tuple(sorted(prices)) != prices or len(set(prices)) != len(prices):
            raise ValueError("hardware-tier prices must increase strictly")
        return self


def load_capacity_selection(
    path: Path = DEFAULT_CAPACITY_SELECTION_PATH,
) -> InfrastructureScalingCapacitySelection:
    """Load the committed, validated hardware ladder without contacting AWS."""
    return InfrastructureScalingCapacitySelection.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def load_experiment_controls(
    path: Path = DEFAULT_EXPERIMENT_CONTROLS_PATH,
    source_benchmark_path: Path = DEFAULT_SOURCE_BENCHMARK_PATH,
) -> InfrastructureScalingControls:
    """Load the committed constant-treatment contract without contacting AWS."""
    controls = InfrastructureScalingControls.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    source_digest = sha256(source_benchmark_path.read_bytes()).hexdigest()
    if controls.workload.source_benchmark_definition_sha256 != source_digest:
        raise ValueError("the source benchmark definition changed after freezing")
    return controls
