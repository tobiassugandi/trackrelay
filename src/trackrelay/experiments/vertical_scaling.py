"""Machine-readable controls for the synchronous vertical-scaling experiment."""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PositiveInteger = Annotated[int, Field(gt=0)]
PositivePrice = Annotated[Decimal, Field(gt=0)]
CONTROL_INSTANCE_TYPE = "c8g.large"
TREATMENT_INSTANCE_TYPE = "c8g.4xlarge"
ALLOWED_INSTANCE_TYPES = (
    CONTROL_INSTANCE_TYPE,
    TREATMENT_INSTANCE_TYPE,
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
    burstable_performance: Literal[False] = False
    on_demand_linux_price_usd_per_hour: PositivePrice


class VerticalScalingCapacitySelection(BaseModel):
    """Freeze the sole treatment variable before cloud-session approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["aws-synchronous-vertical-scaling-capacity-v1"] = (
        "aws-synchronous-vertical-scaling-capacity-v1"
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
    control: Ec2Capacity
    treatment: Ec2Capacity

    @model_validator(mode="after")
    def require_one_clean_vertical_capacity_step(
        self,
    ) -> "VerticalScalingCapacitySelection":
        if self.control.instance_type == self.treatment.instance_type:
            raise ValueError("control and treatment instance types must differ")
        control_family = self.control.instance_type.partition(".")[0]
        treatment_family = self.treatment.instance_type.partition(".")[0]
        if control_family != treatment_family:
            raise ValueError("control and treatment must use the same EC2 family")
        if self.control.processor != self.treatment.processor:
            raise ValueError("control and treatment must use the same processor")
        if self.treatment.vcpu_count <= self.control.vcpu_count:
            raise ValueError("treatment must have more vCPUs than the control")
        if self.treatment.memory_mib <= self.control.memory_mib:
            raise ValueError("treatment must have more memory than the control")
        if (
            self.treatment.on_demand_linux_price_usd_per_hour
            <= self.control.on_demand_linux_price_usd_per_hour
        ):
            raise ValueError("treatment price must exceed the control price")
        return self


def load_capacity_selection(
    path: Path = DEFAULT_CAPACITY_SELECTION_PATH,
) -> VerticalScalingCapacitySelection:
    """Load the committed, validated capacity pair without contacting AWS."""
    return VerticalScalingCapacitySelection.model_validate_json(
        path.read_text(encoding="utf-8")
    )
