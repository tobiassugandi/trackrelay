"""Tests for guarded Stage 9.3 experiment preparation."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import loads
from subprocess import CompletedProcess

from pytest import raises

from tests.test_aws_rehost import GIT_REVISION, completed, make_session
from trackrelay.aws_rehost import AwsRehostError
from trackrelay.aws_session import load_manifest, write_manifest
from trackrelay.aws_vertical_scaling import (
    execute_with_load_window,
    prepare_vertical_scaling_experiment,
)

IMAGE_DIGEST = "sha256:" + "b" * 64


def prepare_rds_session(tmp_path):
    session = make_session(tmp_path, status="rds_correctness_collected")
    manifest = load_manifest(session)
    manifest.update(
        {
            "image": {
                "architecture": "linux/arm64",
                "digest": IMAGE_DIGEST,
                "tag": f"git-{GIT_REVISION[:12]}",
            },
            "rds": {
                "allocated_storage_gib": 20,
                "database_placement": "private-single-az-rds",
                "engine": "postgres",
                "engine_version": "17.6",
                "instance_class": "db.t4g.micro",
                "storage_type": "encrypted-gp3",
            },
        }
    )
    write_manifest(session, manifest)
    return session


def clean_revision_runner(
    arguments: Sequence[str],
    input_text: str | None,
) -> CompletedProcess[str]:
    del input_text
    call = tuple(arguments)
    if call == ("git", "status", "--porcelain"):
        return completed(call)
    if call == ("git", "rev-parse", "HEAD"):
        return completed(call, stdout=GIT_REVISION)
    raise AssertionError(f"unexpected external command: {call}")


def test_preparation_binds_deployment_and_controls_without_contacting_aws(
    tmp_path,
) -> None:
    session = prepare_rds_session(tmp_path)
    prepared_at = datetime(2026, 8, 29, 13, tzinfo=UTC)

    definition = prepare_vertical_scaling_experiment(
        session,
        runner=clean_revision_runner,
        now=lambda: prepared_at,
    )

    assert definition.application_image_digest == IMAGE_DIGEST
    assert definition.rds_engine_version == "17.6"
    assert definition.controls.tier_order == (
        "t4g.small",
        "c8g.large",
        "c8g.4xlarge",
    )
    assert definition.controls.workload.tier_duration_seconds == 180
    assert definition.capacity_selection.vertical_scale.vcpu_count == 16
    definition_path = (
        session.evidence_dir
        / "vertical-scaling"
        / "experiment-definition.json"
    )
    assert definition_path.is_file()
    saved_manifest = load_manifest(session)
    assert saved_manifest["status"] == "vertical_scaling_ready"
    assert saved_manifest["vertical_scaling"]["completed_tiers"] == []
    assert saved_manifest["vertical_scaling"]["current_tier"] == "t4g.small"
    saved_definition = definition_path.read_text(encoding="utf-8")
    assert "123456789012" not in saved_definition
    assert "rds.amazonaws.com" not in saved_definition


def test_preparation_rejects_rds_drift_without_leaving_partial_output(
    tmp_path,
) -> None:
    session = prepare_rds_session(tmp_path)
    manifest = load_manifest(session)
    manifest["rds"]["instance_class"] = "db.t4g.small"
    write_manifest(session, manifest)

    with raises(AwsRehostError, match="RDS settings differ"):
        prepare_vertical_scaling_experiment(
            session,
            runner=clean_revision_runner,
        )

    assert not (session.evidence_dir / "vertical-scaling").exists()
    assert load_manifest(session)["status"] == "rds_correctness_collected"


def test_load_window_excludes_setup_and_metric_collection() -> None:
    test_run_id = "00000000-0000-0000-0000-000000000904"
    started_at = datetime(2026, 8, 29, 13, tzinfo=UTC)
    ended_at = started_at + timedelta(seconds=180)
    clock = iter((started_at, ended_at))
    actions: list[str] = []

    result = execute_with_load_window(
        test_run_id,
        lambda: actions.append("load") or {"exit_code": 0},
        now=lambda: next(clock),
    )

    assert result.value == {"exit_code": 0}
    assert actions == ["load"]
    assert str(result.window.test_run_id) == test_run_id
    assert result.window.started_at == started_at
    assert result.window.ended_at == ended_at


def test_saved_definition_hash_is_bound_into_session_manifest(tmp_path) -> None:
    session = prepare_rds_session(tmp_path)
    prepare_vertical_scaling_experiment(
        session,
        runner=clean_revision_runner,
        now=lambda: datetime(2026, 8, 29, 13, tzinfo=UTC),
    )

    manifest = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["vertical_scaling"]["definition_sha256"]) == 64
