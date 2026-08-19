"""Tests for read-only local experiment resource measurements."""

from datetime import datetime

from fastapi.testclient import TestClient

from trackrelay.main import app
from trackrelay.runtime_metrics import capture_runtime_metrics


def test_capture_runtime_metrics_reports_process_and_pool_values() -> None:
    snapshot = capture_runtime_metrics()

    assert snapshot.process_cpu_seconds >= 0
    assert snapshot.process_max_rss_bytes > 0
    assert snapshot.database_pool.checked_out is not None
    assert snapshot.database_pool.checked_out >= 0


def test_runtime_metrics_endpoint_returns_a_machine_readable_snapshot() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/experiments/runtime-metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert datetime.fromisoformat(body["captured_at"]).tzinfo is not None
    assert body["process_cpu_seconds"] >= 0
    assert body["process_max_rss_bytes"] > 0
    assert body["database_pool"]["checked_out"] >= 0
