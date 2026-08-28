"""Tests for the frozen synchronous vertical-scaling capacity pair."""

from copy import deepcopy
from decimal import Decimal

from pydantic import ValidationError
from pytest import raises

from trackrelay.experiments.vertical_scaling import (
    VerticalScalingCapacitySelection,
    load_capacity_selection,
)


def test_committed_pair_is_an_independent_eightfold_capacity_step() -> None:
    selection = load_capacity_selection()

    assert selection.control.instance_type == "c8g.large"
    assert selection.control.vcpu_count == 2
    assert selection.control.memory_mib == 4096
    assert selection.control.on_demand_linux_price_usd_per_hour == Decimal(
        "0.0916300000"
    )
    assert selection.treatment.instance_type == "c8g.4xlarge"
    assert selection.treatment.vcpu_count == 16
    assert selection.treatment.memory_mib == 32768
    assert selection.treatment.on_demand_linux_price_usd_per_hour == Decimal(
        "0.7330400000"
    )
    assert selection.treatment.vcpu_count / selection.control.vcpu_count == 8
    assert selection.treatment.memory_mib / selection.control.memory_mib == 8


def test_selection_rejects_a_second_instance_family() -> None:
    data = load_capacity_selection().model_dump(mode="json")
    data["treatment"]["instance_type"] = "c7g.4xlarge"

    with raises(ValidationError, match="same EC2 family"):
        VerticalScalingCapacitySelection.model_validate(data)


def test_selection_rejects_a_treatment_that_is_not_larger() -> None:
    data = deepcopy(load_capacity_selection().model_dump(mode="json"))
    data["treatment"]["vcpu_count"] = 2

    with raises(ValidationError, match="more vCPUs"):
        VerticalScalingCapacitySelection.model_validate(data)
