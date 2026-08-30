"""Tests for local performance-experiment preparation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import ValidationError
from pytest import mark, raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import ShipmentStatus
from trackrelay.experiments import performance
from trackrelay.experiments.performance import (
    LoadScenario,
    PerformanceExperimentConfiguration,
    build_k6_command,
    build_performance_manifest,
    derive_performance_result,
    prepare_load_partner,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.models import Partner
from trackrelay.runtime_metrics import (
    DatabasePoolMetrics,
    LogicalCpuTimes,
    RuntimeMetricsSnapshot,
)

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000805")


def create_test_database():
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, create_session_factory(engine)


def test_prepare_load_partner_creates_an_active_alpha_partner() -> None:
    engine, sessions = create_test_database()

    prepare_load_partner("load-alpha", sessions=sessions)

    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        assert partner is not None
        assert partner.name == "Load test load-alpha"
        assert partner.adapter_type == "courier-alpha"
        assert partner.is_active is True
    engine.dispose()


def test_prepare_load_partner_accepts_an_existing_compatible_partner() -> None:
    engine, sessions = create_test_database()
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Existing load partner",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )

    prepare_load_partner("load-alpha", sessions=sessions)

    with sessions() as session:
        partner = session.get(Partner, "load-alpha")
        assert partner is not None
        assert partner.name == "Existing load partner"
    engine.dispose()


def test_prepare_load_partner_rejects_an_incompatible_partner() -> None:
    engine, sessions = create_test_database()
    with sessions.begin() as session:
        session.add(
            Partner(
                id="load-alpha",
                name="Wrong adapter",
                adapter_type="courier-beta",
                is_active=True,
            )
        )

    with raises(
        ValueError,
        match="load partner must be active and use courier-alpha",
    ):
        prepare_load_partner("load-alpha", sessions=sessions)
    engine.dispose()


def test_slow_configuration_declares_every_repeatable_input() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.SLOW,
    )

    assert configuration.expected_request_count == 25
    assert configuration.simulator_mode == "SLOW"
    assert configuration.expected_http_status_code == 201
    assert configuration.model_dump(mode="json")["k6_image"] == (
        "grafana/k6:2.1.0"
    )
    assert configuration.trackrelay_api_url == "http://127.0.0.1:8000"
    assert configuration.trackrelay_api_url_for_container == (
        "http://host.docker.internal:8000"
    )
    assert configuration.downstream_url == "http://127.0.0.1:8001"
    assert configuration.post_load_settle_timeout_seconds == 30
    assert configuration.post_load_stable_window_seconds == 2


def test_outage_configuration_expects_the_persisted_delivery_failure() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.OUTAGE,
    )

    assert configuration.simulator_mode == "UNAVAILABLE"
    assert configuration.expected_http_status_code == 502


def test_healthy_configuration_declares_the_baseline_condition() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
    )

    assert configuration.simulator_mode == "HEALTHY"
    assert configuration.expected_http_status_code == 201

    manifest = build_performance_manifest(
        configuration,
        test_run_id=TEST_RUN_ID,
    )

    assert manifest.scenario_name == "healthy-baseline"
    assert manifest.events_generated == configuration.expected_request_count
    assert len(manifest.expected_final_shipments) == (
        configuration.expected_request_count
    )
    assert set(manifest.expected_final_shipments.values()) == {
        ShipmentStatus.CREATED
    }


def test_configuration_requires_complete_five_event_histories() -> None:
    with raises(
        ValidationError,
        match="request rate times duration must be divisible by 5",
    ):
        PerformanceExperimentConfiguration(
            scenario=LoadScenario.SLOW,
            request_rate_per_second=2,
            duration_seconds=3,
        )

    healthy_configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
        request_rate_per_second=2,
        duration_seconds=3,
    )

    assert healthy_configuration.expected_request_count == 6


def test_performance_manifest_matches_the_scheduled_load() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.OUTAGE,
        request_rate_per_second=1,
        duration_seconds=5,
    )

    manifest = build_performance_manifest(
        configuration,
        test_run_id=TEST_RUN_ID,
    )

    assert manifest.test_run_id == TEST_RUN_ID
    assert manifest.scenario_name == "outage-under-load"
    assert manifest.events_generated == 5
    assert manifest.expected_unique_events == 5
    assert len(manifest.expected_final_shipments) == 1


def test_k6_command_mounts_the_manifest_and_run_directory() -> None:
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.SLOW,
    )
    manifest_path = Path("/tmp/trackrelay-manifest.json")
    run_directory = Path("/tmp/trackrelay-performance-run")

    command = build_k6_command(
        configuration,
        manifest_path=manifest_path,
        run_directory=run_directory,
    )

    assert command[0:3] == ("docker", "run", "--rm")
    assert "LOAD_RATE=5" in command
    assert "LOAD_DURATION_SECONDS=5" in command
    assert "EXPECTED_HTTP_STATUS=201" in command
    assert f"{manifest_path.resolve()}:/input-manifest.json:ro" in command
    assert f"{run_directory.resolve()}:/results" in command
    assert command[-2:] == ("run", "/scripts/fixed-rate.js")


def test_fixed_rate_driver_preallocates_every_permitted_vu() -> None:
    script = (
        Path(__file__).resolve().parents[2] / "load" / "fixed-rate.js"
    ).read_text(encoding="utf-8")

    assert "const preAllocatedVUs = requestRate;" in script
    assert "const maxVUs = requestRate;" in script
    assert "preAllocatedVUs," in script
    assert "maxVUs," in script


def runtime_sample(
    *,
    captured_at: datetime,
    process_cpu_seconds: float,
    checked_out: int,
    available_memory: int,
    cpu0_total: float,
    cpu0_idle: float,
    cpu1_total: float,
    cpu1_idle: float,
) -> RuntimeMetricsSnapshot:
    return RuntimeMetricsSnapshot(
        captured_at=captured_at,
        process_id=7,
        process_cpu_seconds=process_cpu_seconds,
        process_max_rss_bytes=1024,
        python_thread_count=7,
        logical_cpu_count_available=2,
        gil_enabled=True,
        host_logical_cpu_times=(
            LogicalCpuTimes(
                cpu_index=0,
                total_seconds=cpu0_total,
                idle_seconds=cpu0_idle,
            ),
            LogicalCpuTimes(
                cpu_index=1,
                total_seconds=cpu1_total,
                idle_seconds=cpu1_idle,
            ),
        ),
        host_memory_total_bytes=2000,
        host_memory_available_bytes=available_memory,
        database_pool=DatabasePoolMetrics(
            checked_out=checked_out,
            checked_in=5,
            pool_size=5,
            overflow=max(0, checked_out - 5),
            max_overflow=10,
        ),
    )


@mark.parametrize(
    ("failed_request_number", "expected_phase"),
    ((2, "during-load"), (3, "post-load")),
)
def test_overload_runtime_observation_gaps_do_not_erase_the_k6_result(
    monkeypatch,
    failed_request_number: int,
    expected_phase: str,
) -> None:
    started_at = datetime(2026, 8, 30, tzinfo=UTC)
    snapshots = (
        runtime_sample(
            captured_at=started_at,
            process_cpu_seconds=1,
            checked_out=1,
            available_memory=1000,
            cpu0_total=100,
            cpu0_idle=80,
            cpu1_total=100,
            cpu1_idle=90,
        ),
        runtime_sample(
            captured_at=started_at + timedelta(seconds=1),
            process_cpu_seconds=2,
            checked_out=2,
            available_memory=900,
            cpu0_total=101,
            cpu0_idle=80,
            cpu1_total=101,
            cpu1_idle=90,
        ),
    )
    request_count = 0

    def runtime_handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == failed_request_number:
            raise httpx.ReadTimeout("synthetic overload", request=request)
        snapshot = snapshots[0] if request_count == 1 else snapshots[1]
        return httpx.Response(
            200,
            json=snapshot.model_dump(mode="json"),
            request=request,
        )

    class FakeProcess:
        wait_count = 0

        def wait(self, timeout=None):
            del timeout
            self.wait_count += 1
            if self.wait_count == 1:
                raise performance.subprocess.TimeoutExpired("k6", 1)
            return 99

        def poll(self):
            return 99

    monkeypatch.setattr(
        performance.subprocess,
        "Popen",
        lambda _command: FakeProcess(),
    )
    observation_failures = []
    callbacks = []
    with httpx.Client(
        base_url="http://trackrelay.invalid",
        transport=httpx.MockTransport(runtime_handler),
    ) as client:
        exit_code, samples = performance.run_k6_with_resource_sampling(
            ("k6",),
            trackrelay_client=client,
            sample_interval_seconds=1,
            on_load_started=lambda: callbacks.append("started"),
            on_load_ended=lambda: callbacks.append("ended"),
            observation_failures=observation_failures,
        )

    assert exit_code == 99
    assert samples == snapshots
    assert callbacks == ["started", "ended"]
    assert len(observation_failures) == 1
    assert observation_failures[0].phase == expected_phase
    assert observation_failures[0].kind == "timeout"


def test_result_distinguishes_productive_throughput_from_resource_work() -> None:
    started_at = datetime(2026, 8, 29, tzinfo=UTC)
    samples = (
        runtime_sample(
            captured_at=started_at,
            process_cpu_seconds=1,
            checked_out=1,
            available_memory=1000,
            cpu0_total=100,
            cpu0_idle=80,
            cpu1_total=100,
            cpu1_idle=90,
        ),
        runtime_sample(
            captured_at=started_at + timedelta(seconds=10),
            process_cpu_seconds=5,
            checked_out=5,
            available_memory=800,
            cpu0_total=110,
            cpu0_idle=84,
            cpu1_total=110,
            cpu1_idle=99,
        ),
    )
    configuration = PerformanceExperimentConfiguration(
        scenario=LoadScenario.HEALTHY,
        request_rate_per_second=10,
        duration_seconds=10,
    )
    reconciliation = ReconciliationReport(
        test_run_id=TEST_RUN_ID,
        generated=100,
        accepted=100,
        rejected=0,
        unique=100,
        processed=100,
        failed=0,
        pending=0,
        unaccounted=0,
        simulator_receipts=100,
        simulator_unique_events=100,
    )
    summary = {
        "metrics": {
            "http_req_duration": {"values": {"p(95)": 20}},
            "http_req_failed": {"values": {"rate": 0}},
            "http_reqs": {"values": {"count": 100, "rate": 10}},
            "dropped_iterations": {"values": {"count": 0}},
        }
    }

    passing = derive_performance_result(
        configuration,
        test_run_id=TEST_RUN_ID,
        k6_exit_code=0,
        k6_summary=summary,
        resource_samples=samples,
        reconciliation=reconciliation,
    )
    overloaded = derive_performance_result(
        configuration,
        test_run_id=TEST_RUN_ID,
        k6_exit_code=99,
        k6_summary=summary,
        resource_samples=samples,
        reconciliation=reconciliation,
    )

    assert passing.process_average_cpu_cores_used == 0.4
    assert passing.process_average_cpu_capacity_utilization == 0.2
    assert passing.host_per_cpu_average_utilization == (0.6, 0.1)
    assert passing.host_average_cpu_utilization == 0.35
    assert passing.host_minimum_memory_available_bytes == 800
    assert passing.maximum_database_pool_capacity == 15
    assert passing.maximum_database_pool_utilization == 1 / 3
    assert passing.productive_throughput_per_second == 10
    assert overloaded.process_average_cpu_cores_used == 0.4
    assert overloaded.productive_throughput_per_second == 0
