"""Tests for the frozen synchronous vertical-scaling capacity pair."""

from copy import deepcopy
from decimal import Decimal

from pydantic import ValidationError
from pytest import raises

from trackrelay.experiments.vertical_scaling import (
    InfrastructureScalingCapacitySelection,
    load_capacity_selection,
)


def test_committed_ladder_has_three_independently_expected_hardware_roles() -> None:
    selection = load_capacity_selection()

    assert selection.economical_baseline.instance_type == "t4g.small"
    assert selection.economical_baseline.vcpu_count == 2
    assert selection.economical_baseline.memory_mib == 2048
    assert selection.economical_baseline.burstable_performance is True
    assert (
        selection.economical_baseline.on_demand_linux_price_usd_per_hour
        == Decimal("0.0212000000")
    )
    assert selection.workload_fit.instance_type == "c8g.large"
    assert selection.workload_fit.vcpu_count == 2
    assert selection.workload_fit.memory_mib == 4096
    assert selection.workload_fit.burstable_performance is False
    assert selection.workload_fit.on_demand_linux_price_usd_per_hour == Decimal(
        "0.0916300000"
    )
    assert selection.vertical_scale.instance_type == "c8g.4xlarge"
    assert selection.vertical_scale.vcpu_count == 16
    assert selection.vertical_scale.memory_mib == 32768
    assert selection.vertical_scale.burstable_performance is False
    assert selection.vertical_scale.on_demand_linux_price_usd_per_hour == Decimal(
        "0.7330400000"
    )
    assert (
        selection.vertical_scale.vcpu_count
        / selection.workload_fit.vcpu_count
        == 8
    )
    assert (
        selection.vertical_scale.memory_mib
        / selection.workload_fit.memory_mib
        == 8
    )


def test_selection_rejects_a_second_compute_instance_family() -> None:
    data = load_capacity_selection().model_dump(mode="json")
    data["vertical_scale"]["instance_type"] = "c7g.4xlarge"

    with raises(ValidationError, match="same EC2 family"):
        InfrastructureScalingCapacitySelection.model_validate(data)


def test_selection_rejects_a_scale_tier_that_is_not_larger() -> None:
    data = deepcopy(load_capacity_selection().model_dump(mode="json"))
    data["vertical_scale"]["vcpu_count"] = 2

    with raises(ValidationError, match="more vCPUs"):
        InfrastructureScalingCapacitySelection.model_validate(data)


def test_selection_requires_a_burstable_economical_baseline() -> None:
    data = deepcopy(load_capacity_selection().model_dump(mode="json"))
    data["economical_baseline"]["burstable_performance"] = False

    with raises(ValidationError, match="retain burstable"):
        InfrastructureScalingCapacitySelection.model_validate(data)
