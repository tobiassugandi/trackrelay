"""Scaling timing evidence is retained independently of native qualification."""

from json import loads

from tests.test_aws_elasticity_cloudwatch import (
    DIMENSIONS,
    ENDED_AT,
    STARTED_AT,
    completed,
    session,
)
from trackrelay.aws_scaling_diagnostics import collect_scaling_diagnostics


def test_retains_high_resolution_data_and_both_alarm_histories(tmp_path):
    aws_session = session(tmp_path)
    calls = []

    def runner(arguments, input_text=None):
        calls.append(arguments)
        return completed(arguments, stdout='{"retained":true}')

    root = tmp_path / "scaling"
    collect_scaling_diagnostics(
        aws_session, DIMENSIONS, STARTED_AT, ENDED_AT, root, runner=runner
    )
    assert len(calls) == 6
    queries = loads(calls[2][calls[2].index("--metric-data-queries") + 1])
    assert {q["MetricStat"]["Metric"]["MetricName"] for q in queries} == {
        "ArrivalRate",
        "OutstandingEvents",
        "CompletedEvents",
        "QuietSeconds",
        "TelemetryQueryLatency",
        "ServerIngestionLatency",
    }
    assert all(
        q["MetricStat"]["Period"] == 10
        and q["MetricStat"]["Stat"] in ("Maximum", "Minimum", "p95")
        for q in queries
    )
    assert "--include-not-scaled-activities" in calls[3]
    assert calls[4][calls[4].index("--alarm-name") + 1].endswith("-demand-high")
    assert calls[5][calls[5].index("--alarm-name") + 1].endswith("-release-safe")
    assert loads((root / "collection.json").read_text())["complete"]


def test_collection_failure_is_visible_and_does_not_skip_other_evidence(tmp_path):
    aws_session = session(tmp_path)
    calls = []

    def runner(arguments, input_text=None):
        calls.append(arguments)
        if "get-metric-data" in arguments:
            raise RuntimeError("CloudWatch unavailable")
        return completed(arguments, stdout="{}")

    root = tmp_path / "scaling"
    collect_scaling_diagnostics(
        aws_session, DIMENSIONS, STARTED_AT, ENDED_AT, root, runner=runner
    )
    result = loads((root / "collection.json").read_text())
    assert not result["complete"]
    assert result["failures"] == {"high-resolution-metrics": "RuntimeError"}
    assert len(calls) == 6
    assert (root / "scaling-activities.json").is_file()
