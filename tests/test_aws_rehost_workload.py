"""Tests for guarded, endpoint-free AWS rehost workload evidence."""

from collections.abc import Sequence
from datetime import UTC, datetime
from json import loads
from pathlib import Path
from shlex import split
from subprocess import CompletedProcess

from tests.test_aws_rehost import (
    COMMAND_ID,
    GIT_REVISION,
    INSTANCE_ID,
    completed,
    make_session,
    terraform_output_name,
)
from trackrelay.aws_rehost_workload import (
    build_remote_action_payload,
    build_runtime_sampling_payload,
    execute_rehost_workload,
    frozen_rehost_definition,
    start_remote_runtime_sampling,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.experiments.rehost import (
    DeploymentRuntimeSample,
    RehostRuntimeTimeline,
    RehostServerEvidence,
    RehostWorkloadPoint,
    encoded_evidence,
    encoded_runtime_evidence,
)
from trackrelay.runtime_metrics import (
    DatabasePoolMetrics,
    RuntimeMetricsSnapshot,
)

PUBLIC_IP = "198.51.100.25"


def test_frozen_definition_copies_step_8_6_without_local_endpoints() -> None:
    definition = frozen_rehost_definition(
        revision=GIT_REVISION,
        region="ap-southeast-3",
    )

    assert definition.offered_rates_per_second == (10, 25, 50, 100, 250, 500)
    assert definition.tier_duration_seconds == 10
    assert definition.random_seed == 20260806
    assert definition.database_placement == "same-ec2-host-compose-container"
    assert definition.benchmark_driver_placement == "local-developer-machine"
    serialized = definition.model_dump_json()
    assert "127.0.0.1" not in serialized
    assert "host.docker.internal" not in serialized


def test_remote_payload_uses_private_compose_services_without_secrets() -> None:
    point = RehostWorkloadPoint(
        test_run_id="00000000-0000-0000-0000-000000000901",
        request_rate_per_second=10,
        duration_seconds=10,
        partner_id="load-alpha",
    )

    payload = build_remote_action_payload("collect", point)
    command = payload["commands"][0]

    assert "trackrelay.experiments.rehost collect" in command
    assert "--env-file /opt/trackrelay/.env" in command
    assert "POSTGRES_PASSWORD" not in command
    assert "http://" not in command
    assert payload["executionTimeout"] == ["120"]

    runtime_payload = build_runtime_sampling_payload(point)
    runtime_command = runtime_payload["commands"][0]
    assert "trackrelay.experiments.rehost sample-runtime" in runtime_command
    assert "http://" not in runtime_command
    assert "--sampling-duration-seconds 25" in runtime_command
    assert runtime_payload["executionTimeout"] == ["145"]


def test_runtime_sampling_tolerates_more_than_twenty_seconds_of_ssm_delivery(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="rehost_deployed")
    point = RehostWorkloadPoint(
        test_run_id="00000000-0000-0000-0000-000000000902",
        request_rate_per_second=10,
        duration_seconds=180,
        partner_id="load-alpha",
    )
    output_polls = 0
    sleeps: list[float] = []

    def runner(arguments, _input_text):
        nonlocal output_polls
        call = tuple(arguments)
        if "send-command" in call:
            return completed(call, stdout=COMMAND_ID)
        output_polls += 1
        if output_polls <= 41:
            return completed(call)
        return completed(call, stdout="TRACKRELAY_RUNTIME_SAMPLING_READY\n")

    observed_command_id = start_remote_runtime_sampling(
        session,
        instance_id=INSTANCE_ID,
        point=point,
        runner=runner,
        sleeper=sleeps.append,
    )

    assert observed_command_id == COMMAND_ID
    assert output_polls == 42
    assert sleeps == [0.5] * 41


def point_from_payload(path: Path) -> tuple[str, RehostWorkloadPoint]:
    payload = loads(path.read_text(encoding="utf-8"))
    arguments = split(payload["commands"][0].splitlines()[-1])
    module_index = arguments.index("trackrelay.experiments.rehost")
    action = arguments[module_index + 1]

    def value(name: str) -> str:
        return arguments[arguments.index(name) + 1]

    return action, RehostWorkloadPoint(
        test_run_id=value("--test-run-id"),
        request_rate_per_second=int(value("--rate")),
        duration_seconds=int(value("--duration-seconds")),
        random_seed=int(value("--seed")),
        partner_id=value("--partner-id"),
        start_at=value("--start-at"),
        post_load_settle_timeout_seconds=float(
            value("--settle-timeout-seconds")
        ),
        post_load_stable_window_seconds=float(
            value("--stable-window-seconds")
        ),
    )


def test_workload_saves_all_frozen_points_without_the_temporary_endpoint(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path, status="rehost_deployed")
    active_action = ""
    active_point: RehostWorkloadPoint | None = None

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        nonlocal active_action, active_point
        del input_text
        call = tuple(arguments)
        if call == ("git", "status", "--porcelain"):
            return completed(call)
        if call == ("git", "rev-parse", "HEAD"):
            return completed(call, stdout=GIT_REVISION)
        if terraform_output_name(call) == "rehost_instance_id":
            return completed(call, stdout=INSTANCE_ID)
        if terraform_output_name(call) == "rehost_public_ip":
            return completed(call, stdout=PUBLIC_IP)
        if "send-command" in call:
            parameter = call[call.index("--parameters") + 1]
            active_action, active_point = point_from_payload(
                Path(parameter.removeprefix("file://"))
            )
            return completed(call, stdout=COMMAND_ID)
        if "get-command-invocation" in call:
            query = call[call.index("--query") + 1]
            if query == "[Status,ResponseCode]":
                return completed(call, stdout="Success\t0")
            if active_action == "sample-runtime":
                assert active_point is not None
                sample = RuntimeMetricsSnapshot(
                    captured_at=datetime(2026, 8, 26, tzinfo=UTC),
                    process_id=7,
                    process_cpu_seconds=1,
                    process_max_rss_bytes=1024,
                    python_thread_count=1,
                    logical_cpu_count_available=2,
                    gil_enabled=True,
                    host_logical_cpu_times=(),
                    host_memory_total_bytes=None,
                    host_memory_available_bytes=None,
                    database_pool=None,
                )
                timeline = RehostRuntimeTimeline(
                    test_run_id=active_point.test_run_id,
                    sampling_duration_seconds=25,
                    samples=(
                        DeploymentRuntimeSample(
                            api=sample,
                            downstream=sample,
                        ),
                    ),
                )
                return completed(
                    call,
                    stdout=(
                        "TRACKRELAY_RUNTIME_SAMPLING_READY\n"
                        f"{encoded_runtime_evidence(timeline)}\n"
                    ),
                )
            assert active_action == "collect"
            assert active_point is not None
            expected = (
                active_point.request_rate_per_second
                * active_point.duration_seconds
            )
            evidence = RehostServerEvidence(
                point=active_point,
                reconciliation=ReconciliationReport(
                    test_run_id=active_point.test_run_id,
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
            return completed(call, stdout=f"{encoded_evidence(evidence)}\n")
        return completed(call)

    def load_executor(command, client, interval, summary_path):
        del client, interval, summary_path
        rate = int(next(value for value in command if value.startswith("LOAD_RATE=")).split("=")[1])
        duration = int(next(value for value in command if value.startswith("LOAD_DURATION_SECONDS=")).split("=")[1])
        expected = rate * duration
        first_sample = RuntimeMetricsSnapshot(
            captured_at=datetime(2026, 8, 26, tzinfo=UTC),
            process_id=7,
            process_cpu_seconds=1,
            process_max_rss_bytes=1024,
            python_thread_count=1,
            logical_cpu_count_available=2,
            gil_enabled=True,
            host_logical_cpu_times=(),
            host_memory_total_bytes=None,
            host_memory_available_bytes=None,
            database_pool=DatabasePoolMetrics(
                checked_out=0,
                checked_in=1,
                pool_size=5,
                overflow=0,
                max_overflow=10,
            ),
        )
        last_sample = first_sample.model_copy(
            update={
                "captured_at": datetime(2026, 8, 26, 0, 0, 1, tzinfo=UTC),
                "process_cpu_seconds": 1.5,
            }
        )
        return 0, (first_sample, last_sample), {
            "metrics": {
                "http_req_duration": {"values": {"p(95)": 10}},
                "http_req_failed": {"values": {"rate": 0}},
                "http_reqs": {"values": {"count": expected, "rate": rate}},
                "dropped_iterations": {"values": {"count": 0}},
            }
        }

    completed_at = datetime(2026, 8, 26, 12, tzinfo=UTC)
    summary = execute_rehost_workload(
        session,
        runner=runner,
        load_executor=load_executor,
        now=lambda: completed_at,
    )

    assert summary.maximum_sustainable_rate_per_second == 500
    assert len(summary.rate_results) == 6
    saved_session = loads(session.manifest_path.read_text(encoding="utf-8"))
    assert saved_session["status"] == "rehost_workload_collected"
    assert saved_session["rehost_workload"]["result"] == (
        "rehost-workload/summary.json"
    )
    assert len(
        tuple(
            (session.evidence_dir / "rehost-workload").rglob(
                "deployment-runtime-timeline.json"
            )
        )
    ) == 6
    saved_evidence = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (session.evidence_dir / "rehost-workload").rglob("*.json")
    )
    assert PUBLIC_IP not in saved_evidence
    assert "123456789012" not in saved_evidence
