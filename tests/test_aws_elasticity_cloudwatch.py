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
    assert by_id["m_dead_letter_queue_visible"]["ReturnData"] is False
    assert by_id["dead_letter_queue_visible"]["Expression"] == (
        "FILL(m_dead_letter_queue_visible, 0)"
    )
    assert by_id["worker_running_tasks"]["MetricStat"]["Period"] == 60


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
    responses = [
        metric_response(omit_last_bucket_for="rds_cpu"),
        metric_response(),
    ]
    waits: list[float] = []
    observed_return_ids: set[str] = set()

    def runner(arguments: Sequence[str], _input: str | None):
        command = tuple(arguments)
        if command[-1] == "async_observability_dimensions":
            return completed(command, stdout=dumps(DIMENSIONS))
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
    assert observed_return_ids == {
        definition.query_id for definition in metric_definitions()
    }


def test_query_builder_rejects_missing_dimensions() -> None:
    with raises(AwsElasticityCloudWatchError, match="invalid elasticity"):
        build_elasticity_metric_queries(
            {key: value for key, value in DIMENSIONS.items() if key != "rds_identifier"}
        )
