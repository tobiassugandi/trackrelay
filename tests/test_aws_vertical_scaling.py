"""Tests for guarded Stage 9.3 experiment preparation."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import loads
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import httpx
from pytest import raises

from tests.test_aws_rehost import (
    COMMAND_ID,
    GIT_REVISION,
    INSTANCE_ID,
    completed,
    make_session,
    terraform_output_name,
)
from trackrelay.aws_cloudwatch import (
    CloudWatchDatapoint,
    CloudWatchMetricSeries,
    CloudWatchRunEvidence,
    metric_definitions,
)
from trackrelay.aws_rehost import AwsRehostError
from trackrelay.aws_session import load_manifest, write_manifest
from trackrelay.aws_vertical_scaling import (
    execute_timed_local_load,
    execute_with_load_window,
    prepare_vertical_scaling_experiment,
    run_current_vertical_scaling_tier,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    DeploymentRuntimeSample,
    RehostRuntimeTimeline,
    RehostServerEvidence,
)
from trackrelay.runtime_metrics import (
    DatabasePoolMetrics,
    RuntimeMetricsSnapshot,
)

IMAGE_DIGEST = "sha256:" + "b" * 64
PUBLIC_IP = "198.51.100.25"
RDS_IDENTIFIER = "trackrelay-1234abcd-postgres"


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


def test_default_timing_uses_k6_callbacks_not_executor_boundaries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    started_at = datetime(2026, 8, 29, 13, tzinfo=UTC)
    ended_at = started_at + timedelta(seconds=180)
    clock = iter((started_at, ended_at))
    actions: list[str] = []

    def fake_executor(
        _command,
        _client,
        _interval,
        _summary_path,
        *,
        on_load_started,
        on_load_ended,
    ):
        actions.append("pre-load sample")
        on_load_started()
        actions.append("k6")
        on_load_ended()
        actions.append("summary parsing")
        return 0, (), {}

    monkeypatch.setattr(
        "trackrelay.aws_vertical_scaling.execute_local_load",
        fake_executor,
    )
    with httpx.Client() as client:
        result = execute_timed_local_load(
            ("k6", "run"),
            client,
            5,
            tmp_path / "summary.json",
            UUID(int=4),
            lambda: next(clock),
        )

    assert actions == ["pre-load sample", "k6", "summary parsing"]
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


def api_sample(captured_at: datetime, cpu_seconds: float) -> RuntimeMetricsSnapshot:
    return RuntimeMetricsSnapshot(
        captured_at=captured_at,
        process_id=7,
        process_cpu_seconds=cpu_seconds,
        process_max_rss_bytes=1024,
        python_thread_count=4,
        logical_cpu_count_available=2,
        gil_enabled=True,
        host_logical_cpu_times=(),
        host_memory_total_bytes=2048,
        host_memory_available_bytes=1024,
        database_pool=DatabasePoolMetrics(
            checked_out=1,
            checked_in=4,
            pool_size=5,
            overflow=0,
            max_overflow=10,
        ),
    )


def cloudwatch_evidence(
    *,
    test_run_id: UUID,
    started_at: datetime,
    ended_at: datetime,
) -> CloudWatchRunEvidence:
    series = tuple(
        CloudWatchMetricSeries(
            query_id=definition.query_id,
            source=definition.source,
            metric_name=definition.metric_name,
            statistic=definition.statistic,
            unit=definition.unit,
            period_seconds=definition.period_seconds,
            datapoints=(
                CloudWatchDatapoint(
                    interval_started_at=started_at,
                    value=1,
                    load_window_overlap_seconds=min(
                        definition.period_seconds,
                        (ended_at - started_at).total_seconds(),
                    ),
                ),
            ),
        )
        for definition in metric_definitions("t4g.small")
    )
    return CloudWatchRunEvidence(
        test_run_id=test_run_id,
        instance_type="t4g.small",
        load_started_at=started_at,
        load_ended_at=ended_at,
        collected_at=ended_at + timedelta(minutes=1),
        series=series,
    )


def test_current_tier_runner_preserves_every_rate_and_evidence_source(
    tmp_path: Path,
) -> None:
    session = prepare_rds_session(tmp_path)
    prepare_vertical_scaling_experiment(
        session,
        runner=clean_revision_runner,
        now=lambda: datetime(2026, 8, 29, 13, tzinfo=UTC),
    )
    runner_calls: list[tuple[str, ...]] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        runner_calls.append(call)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        output_name = terraform_output_name(call)
        if output_name == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if output_name == "rds_identifier":
            return completed(call, stdout=RDS_IDENTIFIER)
        if output_name == "rehost_public_ip":
            return completed(call, stdout=PUBLIC_IP)
        raise AssertionError(f"unexpected external command: {call}")

    remote_actions: list[tuple[str, int]] = []

    def remote_action(_session, *, action, point, **_kwargs):
        remote_actions.append((action, point.request_rate_per_second))
        if action == "prepare":
            return None
        expected = point.request_rate_per_second * point.duration_seconds
        return RehostServerEvidence(
            point=point,
            reconciliation=ReconciliationReport(
                test_run_id=point.test_run_id,
                generated=expected,
                accepted=expected,
                rejected=0,
                unique=expected,
                processed=expected,
                failed=0,
                pending=0,
                unaccounted=0,
                simulator_receipts=expected,
                simulator_unique_events=expected,
            ),
        )

    def load_executor(
        command,
        _client,
        _interval,
        _summary_path,
        test_run_id,
        now,
    ):
        rate = int(
            next(value for value in command if value.startswith("LOAD_RATE=")).split(
                "=", 1
            )[1]
        )
        duration = int(
            next(
                value
                for value in command
                if value.startswith("LOAD_DURATION_SECONDS=")
            ).split("=", 1)[1]
        )
        expected = rate * duration
        start = datetime(2026, 8, 29, 14, tzinfo=UTC)
        samples = (
            api_sample(start, 1),
            api_sample(start + timedelta(seconds=duration), 2),
        )
        return execute_with_load_window(
            test_run_id,
            lambda: (
                0,
                samples,
                {
                    "metrics": {
                        "http_req_duration": {"values": {"p(95)": 10}},
                        "http_req_failed": {"values": {"rate": 0}},
                        "http_reqs": {
                            "values": {"count": expected, "rate": rate}
                        },
                        "dropped_iterations": {"values": {"count": 0}},
                    }
                },
            ),
            now=now,
        )

    points_by_command: dict[str, object] = {}

    def runtime_starter(_session, *, point, **_kwargs):
        command_id = f"{COMMAND_ID[:-1]}{point.request_rate_per_second % 10}"
        points_by_command[command_id] = point
        return command_id

    def runtime_collector(_session, *, command_id, point, **_kwargs):
        assert points_by_command[command_id] == point
        start = datetime(2026, 8, 29, 14, tzinfo=UTC)
        api = api_sample(start, 1)
        downstream = api.model_copy(update={"process_id": 8, "database_pool": None})
        return RehostRuntimeTimeline(
            test_run_id=point.test_run_id,
            sampling_duration_seconds=195,
            samples=(
                DeploymentRuntimeSample(api=api, downstream=downstream),
                DeploymentRuntimeSample(
                    api=api.model_copy(
                        update={
                            "captured_at": start + timedelta(seconds=195),
                            "process_cpu_seconds": 2,
                        }
                    ),
                    downstream=downstream.model_copy(
                        update={
                            "captured_at": start + timedelta(seconds=195),
                            "process_cpu_seconds": 2,
                        }
                    ),
                ),
            ),
        )

    cloudwatch_windows = []

    def cloudwatch_collector(
        _session,
        *,
        test_run_id,
        load_started_at,
        load_ended_at,
        **_kwargs,
    ):
        cloudwatch_windows.append((load_started_at, load_ended_at))
        return cloudwatch_evidence(
            test_run_id=test_run_id,
            started_at=load_started_at,
            ended_at=load_ended_at,
        )

    clock_values = [datetime(2026, 8, 29, 13, 30, tzinfo=UTC)]
    for index in range(6):
        start = datetime(2026, 8, 29, 14 + index, tzinfo=UTC)
        clock_values.extend((start, start + timedelta(seconds=180)))
    clock_values.append(datetime(2026, 8, 29, 20, 5, tzinfo=UTC))
    clock = iter(clock_values)
    identifiers = iter(UUID(int=index) for index in range(1, 7))

    summary = run_current_vertical_scaling_tier(
        session,
        runner=runner,
        load_executor=load_executor,
        remote_action=remote_action,
        runtime_starter=runtime_starter,
        runtime_collector=runtime_collector,
        cloudwatch_collector=cloudwatch_collector,
        now=lambda: next(clock),
        uuid_factory=lambda: next(identifiers),
    )

    assert summary.maximum_sustainable_rate_per_second == 500
    assert summary.instance_type == "t4g.small"
    assert len(summary.rate_results) == 6
    assert remote_actions == [
        action
        for rate in (10, 25, 50, 100, 250, 500)
        for action in (("prepare", rate), ("collect", rate))
    ]
    assert all((end - start).total_seconds() == 180 for start, end in cloudwatch_windows)
    tier_root = session.evidence_dir / "vertical-scaling" / "tiers" / "t4g.small"
    for evidence_name in (
        "load-window.json",
        "deployment-runtime-timeline.json",
        "server-evidence.json",
        "cloudwatch-metrics.json",
        "performance-result.json",
        "rate-result.json",
    ):
        assert len(tuple(tier_root.rglob(evidence_name))) == 6
    manifest = load_manifest(session)
    assert manifest["status"] == "vertical_scaling_tier_collected"
    assert manifest["vertical_scaling"]["completed_tiers"] == ["t4g.small"]
    assert not any("cloudwatch" in call for call in runner_calls)
    portable_evidence = "\n".join(
        path.read_text(encoding="utf-8") for path in tier_root.rglob("*.json")
    )
    assert PUBLIC_IP not in portable_evidence
    assert INSTANCE_ID not in portable_evidence
    assert RDS_IDENTIFIER not in portable_evidence
