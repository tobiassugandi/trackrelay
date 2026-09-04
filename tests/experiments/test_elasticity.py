"""Tests for the replayable fixed-versus-elastic workload contract."""

from json import loads
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.domain import ShipmentStatus
from trackrelay.experiments.elasticity import (
    ELASTICITY_WORKLOAD_DEFINITION,
    ElasticityTreatment,
    ElasticityWorkloadDefinition,
    ElasticityWorkloadStep,
    build_elasticity_k6_command,
    build_elasticity_manifest,
    prepare_elasticity_workload,
)

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000906")


def test_candidate_is_one_metric_aligned_rise_and_fall() -> None:
    definition = ELASTICITY_WORKLOAD_DEFINITION

    assert tuple(step.offered_rate_per_second for step in definition.steps) == (
        1,
        5,
        10,
        25,
        10,
        5,
        1,
    )
    assert tuple(step.duration_seconds for step in definition.steps) == (
        30,
        30,
        30,
        180,
        30,
        30,
        300,
    )
    assert definition.event_offsets == (0, 30, 180, 480, 4980, 5280, 5430)
    assert definition.expected_request_count == 5730
    assert definition.duration_seconds == 630
    assert definition.peak_rate_per_second == 25
    assert definition.minimum_worker_count == 1
    assert definition.maximum_non_worker_utilization_percent == 70
    assert definition.metric_start_alignment_tolerance_seconds == 1
    assert definition.maximum_database_pool_utilization_percent == 70
    assert definition.maximum_rds_cpu_utilization_percent == 70
    assert definition.maximum_rds_connections == 50
    assert definition.minimum_rds_freeable_memory_bytes == 128 * 1024 * 1024
    assert definition.maximum_rds_read_latency_seconds == 0.02
    assert definition.maximum_rds_write_latency_seconds == 0.02


def test_v3_remains_readable_and_v4_adds_only_recovery_margin():
    current = ElasticityWorkloadDefinition()
    previous = ElasticityWorkloadDefinition(
        name="aws-elasticity-candidate-v3",
        steps=(
            *current.steps[:-1],
            current.steps[-1].model_copy(update={"duration_seconds": 420}),
        ),
    )
    assert previous.duration_seconds == 1140
    assert previous.expected_request_count == 10680
    assert current.duration_seconds - previous.duration_seconds == 120
    assert current.steps[:-1] == previous.steps[:-1]
    with raises(ValueError, match="candidate minimum"):
        ElasticityWorkloadDefinition(steps=previous.steps)


def test_definition_rejects_an_unaligned_or_non_recovering_wave() -> None:
    with raises(ValidationError, match="align to the 60-second metric period"):
        ElasticityWorkloadDefinition(
            steps=(
                ElasticityWorkloadStep(
                    name="baseline",
                    offered_rate_per_second=1,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="rise",
                    offered_rate_per_second=2,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="peak",
                    offered_rate_per_second=3,
                    duration_seconds=61,
                ),
                ElasticityWorkloadStep(
                    name="fall",
                    offered_rate_per_second=2,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="recovery",
                    offered_rate_per_second=1,
                    duration_seconds=300,
                ),
            )
        )

    with raises(ValidationError, match="return to its starting rate"):
        ElasticityWorkloadDefinition(
            steps=(
                ElasticityWorkloadStep(
                    name="baseline",
                    offered_rate_per_second=1,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="rise",
                    offered_rate_per_second=2,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="peak",
                    offered_rate_per_second=4,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="fall",
                    offered_rate_per_second=3,
                    duration_seconds=60,
                ),
                ElasticityWorkloadStep(
                    name="recovery",
                    offered_rate_per_second=2,
                    duration_seconds=300,
                ),
            )
        )


def test_treatments_use_the_same_exact_created_event_shape() -> None:
    definition = ElasticityWorkloadDefinition(
        name="aws-elasticity-candidate-v2",
        steps=(
            ElasticityWorkloadStep(
                name="baseline",
                offered_rate_per_second=1,
                duration_seconds=60,
            ),
            ElasticityWorkloadStep(
                name="rise",
                offered_rate_per_second=2,
                duration_seconds=60,
            ),
            ElasticityWorkloadStep(
                name="peak",
                offered_rate_per_second=3,
                duration_seconds=60,
            ),
            ElasticityWorkloadStep(
                name="fall",
                offered_rate_per_second=2,
                duration_seconds=60,
            ),
            ElasticityWorkloadStep(
                name="recovery",
                offered_rate_per_second=1,
                duration_seconds=300,
            ),
        ),
    )
    fixed = build_elasticity_manifest(
        ElasticityTreatment.FIXED,
        definition=definition,
        test_run_id=TEST_RUN_ID,
    )
    elastic = build_elasticity_manifest(
        ElasticityTreatment.ELASTIC,
        definition=definition,
        test_run_id=TEST_RUN_ID,
    )

    assert fixed.scenario_name == "elasticity-fixed-control"
    assert elastic.scenario_name == "elasticity-elastic-treatment"
    assert fixed.expected_events == elastic.expected_events
    assert fixed.events_generated == definition.expected_request_count
    assert fixed.expected_unique_events == definition.expected_request_count
    assert set(fixed.expected_final_shipments.values()) == {ShipmentStatus.CREATED}


def test_k6_command_mounts_the_definition_manifest_and_results() -> None:
    manifest_path = Path("/tmp/elasticity-manifest.json")
    definition_path = Path("/tmp/elasticity-definition.json")
    result_directory = Path("/tmp/elasticity-results")

    command = build_elasticity_k6_command(
        ELASTICITY_WORKLOAD_DEFINITION,
        manifest_path=manifest_path,
        definition_path=definition_path,
        result_directory=result_directory,
    )

    assert command[0:3] == ("docker", "run", "--rm")
    assert f"{manifest_path.resolve()}:/input-manifest.json:ro" in command
    assert f"{definition_path.resolve()}:/workload-definition.json:ro" in command
    assert f"{result_directory.resolve()}:/results" in command
    assert command[-4:] == (
        "run",
        "--out",
        "json=/results/k6-points.json",
        "/scripts/elasticity-steps.js",
    )

    cloud_command = build_elasticity_k6_command(
        ELASTICITY_WORKLOAD_DEFINITION,
        manifest_path=manifest_path,
        definition_path=definition_path,
        result_directory=result_directory,
        api_url_for_container="http://trackrelay.example.com",
    )
    assert "TRACKRELAY_API_URL=http://trackrelay.example.com" in cloud_command


def test_prepare_writes_self_describing_replay_inputs(tmp_path: Path) -> None:
    output_directory = tmp_path / "fixed"

    definition_path, manifest_path = prepare_elasticity_workload(
        ElasticityTreatment.FIXED,
        output_directory=output_directory,
        test_run_id=TEST_RUN_ID,
    )

    definition = loads(definition_path.read_text(encoding="utf-8"))
    manifest = loads(manifest_path.read_text(encoding="utf-8"))
    command = loads((output_directory / "k6-command.json").read_text(encoding="utf-8"))
    assert definition["name"] == "aws-elasticity-demo-v6"
    assert definition["expected_request_count"] == 5730
    assert manifest["scenario_name"] == "elasticity-fixed-control"
    assert manifest["events_generated"] == 5730
    assert command[-1] == "/scripts/elasticity-steps.js"
