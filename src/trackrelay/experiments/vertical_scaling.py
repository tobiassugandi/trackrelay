"""Machine-readable controls for synchronous infrastructure scaling."""

from decimal import Decimal
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
