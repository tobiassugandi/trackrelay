"""Tests for complete native elasticity metric evidence."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from json import dumps, loads
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

from pytest import raises

from trackrelay.aws_elasticity_cloudwatch import (
    AwsElasticityCloudWatchError,
    build_elasticity_metric_queries,
    collect_elasticity_cloudwatch_evidence,
    expected_bucket_starts,
    metric_definitions,
    parse_elasticity_metric_response,
)
from trackrelay.aws_session import AwsSession
from trackrelay.operator_status import progress_output

STARTED_AT = datetime(2026, 9, 2, 10, 0, 10, tzinfo=UTC)
ENDED_AT = STARTED_AT + timedelta(seconds=180)
TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000946")
DIMENSIONS = {
    "api_service_name": "trackrelay-8a7e37db-api",
    "cluster_name": "trackrelay-8a7e37db-async",
    "dashboard_name": "trackrelay-8a7e37db-async",
    "dead_letter_queue_name": "trackrelay-8a7e37db-delivery-dlq",
    "delivery_queue_name": "trackrelay-8a7e37db-delivery",
    "load_balancer_dimension": "app/trackrelay/1234567890abcdef",
    "rds_identifier": "trackrelay-8a7e37db-postgres",
    "simulator_service_name": "trackrelay-8a7e37db-simulator",
    "worker_service_name": "trackrelay-8a7e37db-worker",
}


def completed(
    arguments: Sequence[str],
    *,
    stdout: str = "",
) -> CompletedProcess[str]:
    return CompletedProcess(arguments, 0, stdout, "")


def session(tmp_path: Path) -> AwsSession:
    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    return AwsSession(
        session_id="cloud-session-4-20260902T090000Z",
        profile="trackrelay-admin",
        region="ap-southeast-3",
        api_ingress_cidr="203.0.113.10/32",
        terraform_dir=terraform_dir,
        evidence_root=tmp_path / "evidence",
        deployment_mode="async",
    )


def metric_response(*, omit_last_bucket_for: str | None = None) -> str:
    timestamps = expected_bucket_starts(STARTED_AT, ENDED_AT)
    return dumps(
        {
            "MetricDataResults": [
                {
                    "Id": definition.query_id,
                    "StatusCode": "Complete",
                    "Timestamps": [
                        value.isoformat()
                        for value in (
                            timestamps[:-1]
                            if definition.query_id == omit_last_bucket_for
                            else timestamps
                        )
                    ],
                    "Values": [
                        1
                        for _value in (
                            timestamps[:-1]
                            if definition.query_id == omit_last_bucket_for
                            else timestamps
                        )
                    ],
                }
                for definition in metric_definitions()
            ]
        }
    )


def test_queries_fill_only_sparse_zero_metrics() -> None:
    queries = build_elasticity_metric_queries(DIMENSIONS)
    by_id = {query["Id"]: query for query in queries}

    assert by_id["alb_requests"]["MetricStat"]["Stat"] == "Sum"
    assert by_id["alb_requests"]["ReturnData"] is True
    assert by_id["m_source_queue_sent"]["MetricStat"]["Metric"]["MetricName"] == (
        "NumberOfMessagesSent"
    )
    assert by_id["m_source_queue_sent"]["MetricStat"]["Stat"] == "Sum"
    assert by_id["source_queue_sent"]["Expression"] == ("FILL(m_source_queue_sent, 0)")
    assert by_id["m_dead_letter_queue_visible"]["ReturnData"] is False
    assert by_id["dead_letter_queue_visible"]["Expression"] == (
        "FILL(m_dead_letter_queue_visible, 0)"
    )
    assert by_id["worker_running_tasks"]["MetricStat"]["Period"] == 60


def test_schema_one_remains_readable_without_the_v3_arrival_series() -> None:
    assert "source_queue_sent" not in {
        definition.query_id for definition in metric_definitions(schema_version=1)
    }
    evidence = parse_elasticity_metric_response(
        dumps(
            {
                "MetricDataResults": [
                    {
                        "Id": definition.query_id,
                        "StatusCode": "Complete",
                        "Timestamps": [
                            value.isoformat()
                            for value in expected_bucket_starts(STARTED_AT, ENDED_AT)
                        ],
                        "Values": [1, 1, 1, 1],
                    }
                    for definition in metric_definitions(schema_version=1)
                ]
            }
        ),
        test_run_id=TEST_RUN_ID,
        window_started_at=STARTED_AT,
        window_ended_at=ENDED_AT,
        collected_at=ENDED_AT + timedelta(minutes=3),
        schema_version=1,
    )
    assert evidence.schema_version == 1


def test_parser_requires_every_overlapping_native_bucket() -> None:
    evidence = parse_elasticity_metric_response(
        metric_response(),
        test_run_id=TEST_RUN_ID,
        window_started_at=STARTED_AT,
        window_ended_at=ENDED_AT,
        collected_at=ENDED_AT + timedelta(minutes=3),
    )

    assert len(evidence.series) == len(metric_definitions())
    assert tuple(
        point.interval_started_at
        for point in evidence.series_by_id()["api_cpu"].datapoints
    ) == expected_bucket_starts(STARTED_AT, ENDED_AT)

    with raises(AwsElasticityCloudWatchError, match="unpublished native buckets"):
        parse_elasticity_metric_response(
            metric_response(omit_last_bucket_for="api_cpu"),
            test_run_id=TEST_RUN_ID,
            window_started_at=STARTED_AT,
            window_ended_at=ENDED_AT,
            collected_at=ENDED_AT + timedelta(minutes=3),
        )


def test_collector_retries_until_full_window_is_published(tmp_path: Path) -> None:
    aws_session = session(tmp_path)
    dimensions = {**DIMENSIONS, "scaling_metrics_namespace": "TrackRelay/Elasticity"}
    responses = [
        metric_response(omit_last_bucket_for="rds_cpu"),
        metric_response(),
    ]
    waits: list[float] = []
    observed_return_ids: set[str] = set()

    def runner(arguments: Sequence[str], _input: str | None):
        command = tuple(arguments)
        if command[-1] == "async_observability_dimensions":
            return completed(command, stdout=dumps(dimensions))
        if (
            "describe-alarm-history" in command
            or "describe-scaling-activities" in command
        ):
            return completed(command, stdout="{}")
        if "--metric-data-queries" in command and not command[
            command.index("--metric-data-queries") + 1
        ].startswith("file://"):
            return completed(command, stdout='{"MetricDataResults": []}')
        if "get-dashboard" in command:
            return completed(
                command,
                stdout=dumps(
                    {
                        "DashboardName": DIMENSIONS["dashboard_name"],
                        "DashboardBody": dumps({"widgets": [{"type": "metric"}] * 7}),
                    }
                ),
            )
        query_path = Path(
            command[command.index("--metric-data-queries") + 1].removeprefix("file://")
        )
        queries = loads(query_path.read_text(encoding="utf-8"))
        observed_return_ids.update(
            query["Id"] for query in queries if query["ReturnData"]
        )
        return completed(command, stdout=responses.pop(0))

    messages = []
    with progress_output(messages.append, repeat_interval_seconds=0):
        evidence = collect_elasticity_cloudwatch_evidence(
            aws_session,
            test_run_id=TEST_RUN_ID,
            window_started_at=STARTED_AT,
            window_ended_at=ENDED_AT,
            runner=runner,
            now=lambda: ENDED_AT + timedelta(minutes=3),
            sleeper=waits.append,
            retry_interval_seconds=2,
        )

    assert evidence.test_run_id == TEST_RUN_ID
    assert waits == [2]
    assert "CloudWatch collection: attempt 1/8" in messages
    assert "CloudWatch collection: attempt 2/8" in messages
    assert any(
        "rds_cpu" in message and "retrying in 2s" in message for message in messages
    )
    assert (
        messages[-1]
        == "CloudWatch evidence complete: all required native buckets present"
    )
    assert not any(DIMENSIONS["cluster_name"] in message for message in messages)
    assert observed_return_ids == {
        definition.query_id for definition in metric_definitions()
    }
    root = next((aws_session.evidence_dir / "diagnostics").rglob("request.json")).parent
    assert (root / "attempt-01.json").read_text() == metric_response(
        omit_last_bucket_for="rds_cpu"
    )
    assert (root / "attempt-02.json").read_text() == metric_response()
    assert loads((root / "collection-status.json").read_text())["complete"]
    scaling_root = (
        aws_session.evidence_dir / "diagnostics" / "scaling" / str(TEST_RUN_ID)
    )
    assert loads((scaling_root / "collection.json").read_text())["complete"]


def test_permanent_simulator_metric_gap_is_preserved_and_not_filled(tmp_path):
    aws_session = session(tmp_path)
    raw = loads(metric_response())
    for series in raw["MetricDataResults"]:
        if series["Id"] in {"simulator_cpu", "simulator_memory"}:
            series["Timestamps"].pop(1)
            series["Values"].pop(1)

    def runner(arguments, _input):
        if arguments[-1] == "async_observability_dimensions":
            return completed(arguments, stdout=dumps(DIMENSIONS))
        if "get-dashboard" in arguments:
            return completed(
                arguments,
                stdout=dumps(
                    {
                        "DashboardName": DIMENSIONS["dashboard_name"],
                        "DashboardBody": dumps({"widgets": [{"type": "metric"}] * 7}),
                    }
                ),
            )
        return completed(arguments, stdout=dumps(raw))

    waits = []
    with raises(AwsElasticityCloudWatchError, match="simulator_cpu.*unpublished"):
        collect_elasticity_cloudwatch_evidence(
            aws_session,
            test_run_id=TEST_RUN_ID,
            window_started_at=STARTED_AT,
            window_ended_at=ENDED_AT,
            runner=runner,
            sleeper=waits.append,
            maximum_attempts=2,
        )
    root = next((aws_session.evidence_dir / "diagnostics").rglob("request.json")).parent
    assert loads((root / "attempt-02.json").read_text()) == raw
    assert not loads((root / "collection-status.json").read_text())["complete"]
    assert waits == [15]


def test_parser_reports_exact_interior_gap_without_filling_it():
    document = loads(metric_response())
    metric = next(
        item for item in document["MetricDataResults"] if item["Id"] == "rds_cpu"
    )
    missing = metric["Timestamps"].pop(1)
    metric["Values"].pop(1)
    with raises(AwsElasticityCloudWatchError) as caught:
        parse_elasticity_metric_response(
            dumps(document),
            test_run_id=TEST_RUN_ID,
            window_started_at=STARTED_AT,
            window_ended_at=ENDED_AT,
            collected_at=ENDED_AT + timedelta(minutes=10),
        )
    assert f"missing UTC: {missing}" in str(caught.value)
    assert f"interior gaps UTC: {missing}" in str(caught.value)
    assert "rds_cpu" in str(caught.value)


def test_query_builder_rejects_missing_dimensions() -> None:
    with raises(AwsElasticityCloudWatchError, match="invalid elasticity"):
        build_elasticity_metric_queries(
            {key: value for key, value in DIMENSIONS.items() if key != "rds_identifier"}
        )
