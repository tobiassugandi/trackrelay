"""Tests for the frozen synchronous infrastructure-scaling controls."""

from copy import deepcopy
from decimal import Decimal

from pydantic import ValidationError
from pytest import raises

from trackrelay.experiments.vertical_scaling import (
    InfrastructureScalingCapacitySelection,
    InfrastructureScalingControls,
    load_capacity_selection,
    load_experiment_controls,
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


def test_committed_controls_freeze_every_non_hardware_input() -> None:
    controls = load_experiment_controls()

    assert controls.only_changed_deployment_input == "ec2-instance-type"
    assert controls.tier_order == (
        "t4g.small",
        "c8g.large",
        "c8g.4xlarge",
    )
    assert controls.application.api_process_count == 1
    assert controls.application.database_pool_size == 5
    assert controls.application.database_max_overflow == 10
    assert controls.rds.instance_class == "db.t4g.micro"
    assert controls.rds.allocated_storage_gib == 20
    assert controls.downstream.cpu_limit == 1.0
    assert controls.downstream.memory_limit_mib == 1024
    assert controls.workload.offered_rates_per_second == (
        10,
        25,
        50,
        100,
        250,
        500,
    )
    assert controls.workload.tier_duration_seconds == 180
    assert controls.workload.runtime_sample_interval_seconds == 5


def test_controls_reject_a_changed_workload_rate() -> None:
    data = deepcopy(load_experiment_controls().model_dump(mode="json"))
    data["workload"]["offered_rates_per_second"][-1] = 1000

    with raises(ValidationError, match="rate ladder"):
        InfrastructureScalingControls.model_validate(data)


def test_controls_reject_a_changed_pool_size() -> None:
    data = deepcopy(load_experiment_controls().model_dump(mode="json"))
    data["application"]["database_pool_size"] = 10

    with raises(ValidationError, match="Input should be 5"):
        InfrastructureScalingControls.model_validate(data)


def test_controls_reject_a_changed_source_benchmark(tmp_path) -> None:
    changed_source = tmp_path / "benchmark-definition.json"
    changed_source.write_text("{}\n", encoding="utf-8")

    with raises(ValueError, match="source benchmark definition changed"):
        load_experiment_controls(source_benchmark_path=changed_source)
