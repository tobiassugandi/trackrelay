"""Tests for read-only local experiment resource measurements."""

from datetime import datetime

from fastapi.testclient import TestClient

from trackrelay.main import app
from trackrelay.runtime_metrics import (
    capture_runtime_metrics,
    parse_linux_logical_cpu_times,
    parse_linux_memory_bytes,
)


def test_capture_runtime_metrics_reports_process_and_pool_values() -> None:
    snapshot = capture_runtime_metrics()

    assert snapshot.process_id > 0
    assert snapshot.process_cpu_seconds >= 0
    assert snapshot.process_max_rss_bytes > 0
    assert snapshot.python_thread_count > 0
    assert snapshot.logical_cpu_count_available > 0
    assert snapshot.gil_enabled is True
    assert snapshot.database_pool.checked_out is not None
    assert snapshot.database_pool.checked_out >= 0
    assert snapshot.database_pool.max_overflow == 10


def test_linux_host_metrics_are_parsed_into_comparable_counters() -> None:
    cpu_times = parse_linux_logical_cpu_times(
        "cpu  300 0 100 600 20 0 0 0\n"
        "cpu0 100 0 20 300 10 0 0 0\n"
        "cpu1 200 0 80 300 10 0 0 0\n"
        "intr 1",
        clock_ticks_per_second=100,
    )
    total_memory, available_memory = parse_linux_memory_bytes(
        "MemTotal:       16384 kB\nMemAvailable:    4096 kB\n"
    )

    assert [sample.cpu_index for sample in cpu_times] == [0, 1]
    assert cpu_times[0].total_seconds == 4.3
    assert cpu_times[0].idle_seconds == 3.1
    assert total_memory == 16 * 1024**2
    assert available_memory == 4 * 1024**2


def test_runtime_metrics_endpoint_returns_a_machine_readable_snapshot() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/experiments/runtime-metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 2
    assert datetime.fromisoformat(body["captured_at"]).tzinfo is not None
    assert body["process_id"] > 0
    assert body["process_cpu_seconds"] >= 0
    assert body["process_max_rss_bytes"] > 0
    assert body["python_thread_count"] > 0
    assert body["logical_cpu_count_available"] > 0
    assert body["database_pool"]["checked_out"] >= 0
