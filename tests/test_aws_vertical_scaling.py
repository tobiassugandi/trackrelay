"""Tests for guarded Stage 9.3 experiment preparation."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import httpx
from pytest import mark, raises

from tests.test_aws_rehost import (
    COMMAND_ID,
    GIT_REVISION,
    INSTANCE_ID,
    REPOSITORY_URL,
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
from trackrelay.aws_rehost_workload import (
    RemoteRuntimeSampler,
    runtime_sampler_container_name,
)
from trackrelay.aws_session import load_manifest, write_manifest
from trackrelay.aws_vertical_scaling import (
    execute_timed_local_load,
    execute_with_load_window,
    prepare_vertical_scaling_experiment,
    run_current_vertical_scaling_tier,
    run_remote_experiment_reset,
    transition_to_next_vertical_scaling_tier,
    validate_transition_plan,
)
from trackrelay.experiments.performance import PerformanceExperimentResult
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    DeploymentRuntimeSample,
    ExperimentTableCounts,
    RehostExperimentResetEvidence,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    encoded_reset_evidence,
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
                "architecture": "linux/amd64",
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
        "t3.small",
        "c7i-flex.large",
        "m7i-flex.large",
    )
    assert definition.controls.workload.tier_duration_seconds == 180
    assert definition.capacity_selection.memory_optimized.vcpu_count == 2
    definition_path = (
        session.evidence_dir
        / "vertical-scaling"
        / "experiment-definition.json"
    )
    assert definition_path.is_file()
    saved_manifest = load_manifest(session)
    assert saved_manifest["status"] == "vertical_scaling_ready"
    assert saved_manifest["vertical_scaling"]["completed_tiers"] == []
    assert saved_manifest["vertical_scaling"]["current_tier"] == "t3.small"
    saved_definition = definition_path.read_text(encoding="utf-8")
    assert "123456789012" not in saved_definition
    assert "rds.amazonaws.com" not in saved_definition


def test_preparation_rejects_rds_drift_without_leaving_partial_output(
    tmp_path,
) -> None:
    session = prepare_rds_session(tmp_path)
    manifest = load_manifest(session)
    manifest["rds"]["instance_class"] = "db.t3.small"
    write_manifest(session, manifest)

    with raises(AwsRehostError, match="RDS settings differ"):
        prepare_vertical_scaling_experiment(
            session,
            runner=clean_revision_runner,
        )

    assert not (session.evidence_dir / "vertical-scaling").exists()
    assert load_manifest(session)["status"] == "rds_correctness_collected"


def test_preparation_rejects_an_arm_image_for_the_x86_ladder(tmp_path) -> None:
    session = prepare_rds_session(tmp_path)
    manifest = load_manifest(session)
    manifest["image"]["architecture"] = "linux/arm64"
    write_manifest(session, manifest)

    with raises(AwsRehostError, match="requires the x86_64 image"):
        prepare_vertical_scaling_experiment(
            session,
            runner=clean_revision_runner,
        )

    assert not (session.evidence_dir / "vertical-scaling").exists()


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
            on_load_ended=lambda: actions.append("sampler stop"),
        )

    assert actions == [
        "pre-load sample",
        "k6",
        "sampler stop",
        "summary parsing",
    ]
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
        for definition in metric_definitions("t3.small")
    )
    return CloudWatchRunEvidence(
        test_run_id=test_run_id,
        instance_type="t3.small",
        load_started_at=started_at,
        load_ended_at=ended_at,
        collected_at=ended_at + timedelta(minutes=1),
        series=series,
    )


@mark.parametrize(
    ("first_failing_rate", "executed_rates"),
    (
        (None, (10, 25, 50, 100, 250, 500)),
        (50, (10, 25, 50)),
    ),
)
def test_current_tier_runner_preserves_evidence_and_stops_at_first_failure(
    tmp_path: Path,
    first_failing_rate: int | None,
    executed_rates: tuple[int, ...],
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
        *,
        on_load_ended,
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
        result = execute_with_load_window(
            test_run_id,
            lambda: (
                99 if rate == first_failing_rate else 0,
                samples,
                {
                    "metrics": {
                        "http_req_duration": {"values": {"p(95)": 10}},
                        "http_req_failed": {"values": {"rate": 0}},
                        "http_reqs": {
                            "values": {"count": expected, "rate": rate}
                        },
                        "dropped_iterations": {
                            "values": {
                                "count": 1 if rate == first_failing_rate else 0
                            }
                        },
                    }
                },
            ),
            now=now,
        )
        on_load_ended()
        return result

    def runtime_starter(_session, *, point, **_kwargs):
        rate_index = (10, 25, 50, 100, 250, 500).index(
            point.request_rate_per_second
        )
        return RemoteRuntimeSampler(
            container_name=runtime_sampler_container_name(point),
            ready_at=datetime(
                2026,
                8,
                29,
                14 + rate_index,
                tzinfo=UTC,
            ),
        )

    stopped_rates = []

    def runtime_stopper(_session, *, sampler, point, **_kwargs):
        assert sampler.container_name == runtime_sampler_container_name(point)
        stopped_rates.append(point.request_rate_per_second)

    def runtime_collector(_session, *, sampler, point, **_kwargs):
        assert sampler.container_name == runtime_sampler_container_name(point)
        start = sampler.ready_at
        api = api_sample(start, 1)
        downstream = api.model_copy(update={"process_id": 8, "database_pool": None})
        return RehostRuntimeTimeline(
            test_run_id=point.test_run_id,
            sampling_timeout_seconds=210,
            samples=(
                DeploymentRuntimeSample(api=api, downstream=downstream),
                DeploymentRuntimeSample(
                    api=api.model_copy(
                        update={
                            "captured_at": start + timedelta(seconds=210),
                            "process_cpu_seconds": 2,
                        }
                    ),
                    downstream=downstream.model_copy(
                        update={
                            "captured_at": start + timedelta(seconds=210),
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
        runtime_stopper=runtime_stopper,
        runtime_collector=runtime_collector,
        cloudwatch_collector=cloudwatch_collector,
        now=lambda: next(clock),
        uuid_factory=lambda: next(identifiers),
    )

    assert summary.maximum_sustainable_rate_per_second == (
        500 if first_failing_rate is None else 25
    )
    assert summary.first_failing_rate_per_second == first_failing_rate
    assert summary.instance_type == "t3.small"
    assert tuple(
        result.offered_rate_per_second for result in summary.rate_results
    ) == executed_rates
    assert stopped_rates == list(executed_rates)
    assert remote_actions == [
        action
        for rate in executed_rates
        for action in (("prepare", rate), ("collect", rate))
    ]
    assert all((end - start).total_seconds() == 180 for start, end in cloudwatch_windows)
    tier_root = session.evidence_dir / "vertical-scaling" / "tiers" / "t3.small"
    for evidence_name in (
        "load-window.json",
        "deployment-runtime-timeline.json",
        "server-evidence.json",
        "cloudwatch-metrics.json",
        "performance-result.json",
        "rate-result.json",
    ):
        assert len(tuple(tier_root.rglob(evidence_name))) == len(executed_rates)
    for performance_path in tier_root.rglob("performance-result.json"):
        PerformanceExperimentResult.model_validate_json(
            performance_path.read_text(encoding="utf-8")
        )
    manifest = load_manifest(session)
    assert manifest["status"] == "vertical_scaling_tier_collected"
    assert manifest["vertical_scaling"]["completed_tiers"] == ["t3.small"]
    assert not any("cloudwatch" in call for call in runner_calls)
    portable_evidence = "\n".join(
        path.read_text(encoding="utf-8") for path in tier_root.rglob("*.json")
    )
    assert PUBLIC_IP not in portable_evidence
    assert INSTANCE_ID not in portable_evidence
    assert RDS_IDENTIFIER not in portable_evidence


def test_current_tier_runner_cleans_the_sampler_when_load_execution_fails(
    tmp_path: Path,
) -> None:
    session = prepare_rds_session(tmp_path)
    prepared_at = datetime(2026, 8, 29, 13, tzinfo=UTC)
    prepare_vertical_scaling_experiment(
        session,
        runner=clean_revision_runner,
        now=lambda: prepared_at,
    )

    def runner(arguments, input_text):
        call = tuple(arguments)
        if call in (
            ("git", "status", "--porcelain"),
            ("git", "rev-parse", "HEAD"),
        ):
            return clean_revision_runner(arguments, input_text)
        outputs = {
            "rehost_instance_id": INSTANCE_ID,
            "rds_identifier": RDS_IDENTIFIER,
            "rehost_public_ip": PUBLIC_IP,
        }
        output_name = terraform_output_name(call)
        if output_name in outputs:
            return completed(call, stdout=outputs[output_name])
        raise AssertionError(f"unexpected external command: {call}")

    point_holder = []

    def runtime_starter(_session, *, point, **_kwargs):
        point_holder.append(point)
        return RemoteRuntimeSampler(
            container_name=runtime_sampler_container_name(point),
            ready_at=datetime(2026, 8, 29, 14, tzinfo=UTC),
        )

    cleaned = []

    def runtime_cleaner(_session, *, point, **_kwargs):
        cleaned.append(point)
        raise OSError("synthetic cleanup failure")

    def fail_load(*_args, **_kwargs):
        raise RuntimeError("synthetic load failure")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("post-load collection must not run")

    with raises(RuntimeError, match="synthetic load failure") as error:
        run_current_vertical_scaling_tier(
            session,
            runner=runner,
            load_executor=fail_load,
            remote_action=lambda *_args, **_kwargs: None,
            runtime_starter=runtime_starter,
            runtime_collector=unexpected,
            runtime_cleaner=runtime_cleaner,
            cloudwatch_collector=unexpected,
            now=lambda: prepared_at,
            uuid_factory=lambda: UUID(int=1),
        )

    assert cleaned == point_holder
    assert error.value.__notes__ == [
        "detached runtime sampler cleanup also failed: OSError"
    ]


def prepared_completed_baseline_session(tmp_path: Path):
    """Prepare a session whose first RDS-backed hardware tier is complete."""
    session = prepare_rds_session(tmp_path)
    prepare_vertical_scaling_experiment(
        session,
        runner=clean_revision_runner,
        now=lambda: datetime(2026, 8, 29, 13, tzinfo=UTC),
    )
    manifest = load_manifest(session)
    manifest["status"] = "vertical_scaling_tier_collected"
    manifest["vertical_scaling"]["completed_tiers"] = ["t3.small"]
    write_manifest(session, manifest)
    return session


def reset_evidence() -> RehostExperimentResetEvidence:
    empty = ExperimentTableCounts(
        delivery_attempts=0,
        events=0,
        shipments=0,
        test_runs=0,
    )
    return RehostExperimentResetEvidence(
        completed_at=datetime(2026, 8, 29, 21, tzinfo=UTC),
        database_rows_removed=ExperimentTableCounts(
            delivery_attempts=4,
            events=3,
            shipments=2,
            test_runs=1,
        ),
        database_rows_remaining=empty,
        downstream_receipts_removed=3,
    )


def test_remote_reset_reads_only_the_framed_ssm_evidence(tmp_path: Path) -> None:
    session = prepare_rds_session(tmp_path)
    expected = reset_evidence()

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        if "send-command" in call:
            return completed(call, stdout=COMMAND_ID)
        if "wait" in call and "command-executed" in call:
            return completed(call)
        if "get-command-invocation" in call and "[Status,ResponseCode]" in call:
            return completed(call, stdout="Success\t0")
        if "get-command-invocation" in call and "StandardOutputContent" in call:
            return completed(
                call,
                stdout=(
                    "ordinary compose output\n"
                    f"{encoded_reset_evidence(expected)}\n"
                ),
            )
        raise AssertionError(f"unexpected external command: {call}")

    observed = run_remote_experiment_reset(
        session,
        instance_id=INSTANCE_ID,
        runner=runner,
    )

    assert observed == expected


def test_transition_applies_only_saved_next_tier_plan_and_revalidates(
    tmp_path: Path,
) -> None:
    session = prepared_completed_baseline_session(tmp_path)
    calls: list[tuple[str, ...]] = []
    applied_plan_paths: list[str] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        output_name = terraform_output_name(call)
        if output_name == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if output_name == "rds_identifier":
            return completed(call, stdout=RDS_IDENTIFIER)
        if output_name == "rehost_ecr_repository_url":
            return completed(call, stdout=REPOSITORY_URL)
        if "plan" in call:
            assert "-var=rehost_instance_type=c7i-flex.large" in call
            output_argument = next(value for value in call if value.startswith("-out="))
            Path(output_argument.removeprefix("-out=")).write_bytes(b"saved plan")
            return completed(call, stdout="one resource changed")
        if "show" in call and "-json" in call:
            plan = {
                "resource_changes": [
                    {
                        "address": "aws_db_instance.postgres",
                        "change": {"actions": ["no-op"]},
                    },
                    {
                        "address": "aws_instance.rehost",
                        "change": {
                            "actions": ["update"],
                            "before": {
                                "instance_type": "t3.small",
                                "credit_specification": [
                                    {"cpu_credits": "standard"}
                                ],
                            },
                            "after": {
                                "instance_type": "c7i-flex.large",
                                "credit_specification": [],
                            },
                        },
                    },
                ]
            }
            return completed(call, stdout=dumps(plan))
        if "apply" in call:
            assert "-var=rehost_instance_type=c7i-flex.large" not in call
            applied_plan_paths.append(call[-1])
            return completed(call, stdout="apply complete")
        if "ec2" in call and "describe-instances" in call:
            return completed(call, stdout="c7i-flex.large")
        raise AssertionError(f"unexpected external command: {call}")

    reset_calls: list[tuple[str, str]] = []

    def resetter(reset_session, *, instance_id, **_kwargs):
        reset_calls.append((reset_session.rehost_instance_type, instance_id))
        return reset_evidence()

    wait_calls: list[tuple[str, str]] = []

    def waiter(wait_session, instance_id, **_kwargs):
        wait_calls.append((wait_session.rehost_instance_type, instance_id))

    validation_calls: list[tuple[str, str, str]] = []

    def validator(
        validation_session,
        *,
        instance_id,
        image_reference,
        **_kwargs,
    ):
        validation_calls.append(
            (
                validation_session.rehost_instance_type,
                instance_id,
                image_reference,
            )
        )
        return COMMAND_ID

    clock = iter(
        (
            datetime(2026, 8, 29, 21, 5, tzinfo=UTC),
            datetime(2026, 8, 29, 21, 7, tzinfo=UTC),
        )
    )
    evidence = transition_to_next_vertical_scaling_tier(
        session,
        target_instance_type="c7i-flex.large",
        approved_session_id=session.session_id,
        approved_target_instance_type="c7i-flex.large",
        runner=runner,
        resetter=resetter,
        ssm_waiter=waiter,
        deployment_validator=validator,
        now=lambda: next(clock),
    )

    assert evidence.source_instance_type == "t3.small"
    assert evidence.target_instance_type == "c7i-flex.large"
    assert evidence.plan.changed_attributes == (
        "credit_specification",
        "instance_type",
    )
    assert reset_calls == [("t3.small", INSTANCE_ID)]
    assert wait_calls == [("c7i-flex.large", INSTANCE_ID)]
    assert validation_calls == [
        ("c7i-flex.large", INSTANCE_ID, f"{REPOSITORY_URL}@{IMAGE_DIGEST}")
    ]
    assert applied_plan_paths == [
        str(
            session.evidence_dir
            / "vertical-scaling"
            / "transitions"
            / "t3.small-to-c7i-flex.large"
            / "terraform-transition.tfplan"
        )
    ]
    target_session = session.__class__(
        **{
            **session.__dict__,
            "rehost_instance_type": "c7i-flex.large",
        }
    )
    manifest = load_manifest(target_session)
    assert manifest["status"] == "vertical_scaling_ready"
    assert manifest["rehost_instance_type"] == "c7i-flex.large"
    assert manifest["vertical_scaling"]["current_tier"] == "c7i-flex.large"
    assert manifest["vertical_scaling"]["completed_tiers"] == ["t3.small"]
    assert "pending_transition" not in manifest["vertical_scaling"]
    assert len(manifest["vertical_scaling"]["transitions"]) == 1
    transition_root = (
        session.evidence_dir
        / "vertical-scaling"
        / "transitions"
        / "t3.small-to-c7i-flex.large"
    )
    assert (transition_root / "reset-evidence.json").is_file()
    assert (transition_root / "plan-evidence.json").is_file()
    assert (transition_root / "transition-evidence.json").is_file()
    portable_evidence = (transition_root / "transition-evidence.json").read_text(
        encoding="utf-8"
    )
    assert INSTANCE_ID not in portable_evidence
    assert RDS_IDENTIFIER not in portable_evidence
    assert REPOSITORY_URL not in portable_evidence
    assert not any("ssm" in call for call in calls)


def test_transition_preserves_failed_terraform_apply_output(tmp_path: Path) -> None:
    session = prepared_completed_baseline_session(tmp_path)

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        output_name = terraform_output_name(call)
        if output_name == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if output_name == "rds_identifier":
            return completed(call, stdout=RDS_IDENTIFIER)
        if "plan" in call:
            output_argument = next(value for value in call if value.startswith("-out="))
            Path(output_argument.removeprefix("-out=")).write_bytes(b"saved plan")
            return completed(call, stdout="one resource changed")
        if "show" in call and "-json" in call:
            return completed(
                call,
                stdout=dumps(
                    {
                        "resource_changes": [
                            {
                                "address": "aws_instance.rehost",
                                "change": {
                                    "actions": ["update"],
                                    "before": {
                                        "instance_type": "t3.small",
                                        "credit_specification": [
                                            {"cpu_credits": "standard"}
                                        ],
                                    },
                                    "after": {
                                        "instance_type": "c7i-flex.large",
                                        "credit_specification": [],
                                    },
                                },
                            }
                        ]
                    }
                ),
            )
        if "apply" in call:
            return CompletedProcess(
                call,
                1,
                "stopped the source instance\n",
                "free-plan instance change rejected\n",
            )
        raise AssertionError(f"unexpected external command: {call}")

    with raises(AwsRehostError, match="transition apply failed"):
        transition_to_next_vertical_scaling_tier(
            session,
            target_instance_type="c7i-flex.large",
            approved_session_id=session.session_id,
            approved_target_instance_type="c7i-flex.large",
            runner=runner,
            resetter=lambda *_args, **_kwargs: reset_evidence(),
        )

    apply_log = (
        session.evidence_dir
        / "vertical-scaling"
        / "transitions"
        / "t3.small-to-c7i-flex.large"
        / "terraform-apply.log"
    ).read_text(encoding="utf-8")
    assert "exit_code: 1" in apply_log
    assert "stopped the source instance" in apply_log
    assert "free-plan instance change rejected" in apply_log


def test_transition_rejects_mismatched_approval_before_external_work(
    tmp_path: Path,
) -> None:
    session = prepared_completed_baseline_session(tmp_path)
    external_work: list[object] = []

    with raises(AwsRehostError, match="approved transition target"):
        transition_to_next_vertical_scaling_tier(
            session,
            target_instance_type="c7i-flex.large",
            approved_session_id=session.session_id,
            approved_target_instance_type="m7i-flex.large",
            runner=lambda *_args, **_kwargs: external_work.append("runner"),
            resetter=lambda *_args, **_kwargs: external_work.append("reset"),
        )

    assert external_work == []


def test_transition_plan_rejects_an_unrelated_resource_change() -> None:
    resource_changes = [
        {
            "address": "aws_instance.rehost",
            "change": {
                "actions": ["update"],
                "before": {"instance_type": "t3.small"},
                "after": {"instance_type": "c7i-flex.large"},
            },
        },
        {
            "address": "aws_db_instance.postgres",
            "change": {
                "actions": ["update"],
                "before": {"instance_class": "db.t4g.micro"},
                "after": {"instance_class": "db.t3.small"},
            },
        },
    ]

    with raises(AwsRehostError, match="exactly one resource"):
        validate_transition_plan(
            dumps({"resource_changes": resource_changes}),
            source_instance_type="t3.small",
            target_instance_type="c7i-flex.large",
            plan_sha256="a" * 64,
        )


def test_transition_plan_accepts_computed_public_address_restart_effects() -> None:
    resource_changes = [
        {
            "address": "aws_instance.rehost",
            "change": {
                "actions": ["update"],
                "before": {
                    "instance_type": "t3.small",
                    "primary_network_interface_id": "eni-0123456789abcdef0",
                    "private_ip": "10.42.1.43",
                    "public_dns": "ec2-198-51-100-20.example.invalid",
                    "public_ip": "198.51.100.20",
                },
                "after": {
                    "instance_type": "c7i-flex.large",
                    "primary_network_interface_id": "eni-0123456789abcdef0",
                    "private_ip": "10.42.1.43",
                },
                "after_unknown": {
                    "public_dns": True,
                    "public_ip": True,
                },
            },
        }
    ]

    evidence = validate_transition_plan(
        dumps({"resource_changes": resource_changes}),
        source_instance_type="t3.small",
        target_instance_type="c7i-flex.large",
        plan_sha256="a" * 64,
    )

    assert evidence.changed_attributes == (
        "instance_type",
        "public_dns",
        "public_ip",
    )


def test_transition_plan_rejects_a_concrete_public_address_change() -> None:
    resource_changes = [
        {
            "address": "aws_instance.rehost",
            "change": {
                "actions": ["update"],
                "before": {
                    "instance_type": "t3.small",
                    "public_ip": "198.51.100.20",
                },
                "after": {
                    "instance_type": "c7i-flex.large",
                    "public_ip": "198.51.100.21",
                },
                "after_unknown": {"public_ip": False},
            },
        }
    ]

    with raises(AwsRehostError, match="unexpected public address"):
        validate_transition_plan(
            dumps({"resource_changes": resource_changes}),
            source_instance_type="t3.small",
            target_instance_type="c7i-flex.large",
            plan_sha256="a" * 64,
        )
