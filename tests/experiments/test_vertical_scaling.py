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

    assert selection.economical_baseline.instance_type == "t3.small"
    assert selection.economical_baseline.vcpu_count == 2
    assert selection.economical_baseline.memory_mib == 2048
    assert selection.economical_baseline.burstable_performance is True
    assert (
        selection.economical_baseline.on_demand_linux_price_usd_per_hour
        == Decimal("0.0264000000")
    )
    assert selection.compute_optimized.instance_type == "c7i-flex.large"
    assert selection.compute_optimized.vcpu_count == 2
    assert selection.compute_optimized.memory_mib == 4096
    assert selection.compute_optimized.burstable_performance is False
    assert selection.compute_optimized.on_demand_linux_price_usd_per_hour == Decimal(
        "0.0977500000"
    )
    assert selection.memory_optimized.instance_type == "m7i-flex.large"
    assert selection.memory_optimized.vcpu_count == 2
    assert selection.memory_optimized.memory_mib == 8192
    assert selection.memory_optimized.burstable_performance is False
    assert selection.memory_optimized.on_demand_linux_price_usd_per_hour == Decimal(
        "0.1197000000"
    )
    assert (
        selection.memory_optimized.vcpu_count
        / selection.compute_optimized.vcpu_count
        == 1
    )
    assert (
        selection.memory_optimized.memory_mib
        / selection.compute_optimized.memory_mib
        == 2
    )


def test_selection_rejects_a_different_flex_processor() -> None:
    data = load_capacity_selection().model_dump(mode="json")
    data["memory_optimized"]["processor"] = "different processor"

    with raises(ValidationError, match="same processor"):
        InfrastructureScalingCapacitySelection.model_validate(data)


def test_selection_rejects_a_changed_vcpu_count() -> None:
    data = deepcopy(load_capacity_selection().model_dump(mode="json"))
    data["memory_optimized"]["vcpu_count"] = 4

    with raises(ValidationError, match="same vCPU count"):
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
        "t3.small",
        "c7i-flex.large",
        "m7i-flex.large",
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
    assert controls.workload.ec2_rds_cloudwatch_period_seconds == 60
    assert controls.workload.cpu_credit_cloudwatch_period_seconds == 300
    assert controls.workload.post_load_settle_timeout_seconds == 30
    assert controls.workload.post_load_stable_window_seconds == 2


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
