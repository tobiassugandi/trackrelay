"""Tests for aligned, resolution-aware AWS resource evidence."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

from pytest import raises

from tests.test_aws_rehost import INSTANCE_ID, completed, make_session
from trackrelay.aws_cloudwatch import (
    build_cloudwatch_query,
    collect_cloudwatch_evidence,
    metric_definitions,
    parse_cloudwatch_response,
)
from trackrelay.aws_rehost import AwsRehostError

TEST_RUN_ID = "00000000-0000-0000-0000-000000000903"
RDS_IDENTIFIER = "trackrelay-1234abcd-postgres"
LOAD_STARTED_AT = datetime(2026, 8, 29, 12, 2, 10, tzinfo=UTC)
LOAD_ENDED_AT = datetime(2026, 8, 29, 12, 5, 10, tzinfo=UTC)


def cloudwatch_response(instance_type: str) -> str:
    results = []
    for definition in metric_definitions(instance_type):
        if definition.period_seconds == 300:
            timestamps = ["2026-08-29T12:00:00Z", "2026-08-29T12:05:00Z"]
        else:
            timestamps = [
                "2026-08-29T12:02:00Z",
                "2026-08-29T12:03:00Z",
                "2026-08-29T12:04:00Z",
                "2026-08-29T12:05:00Z",
            ]
        results.append(
            {
                "Id": definition.query_id,
                "StatusCode": "Complete",
                "Timestamps": timestamps,
                "Values": [float(index + 1) for index in range(len(timestamps))],
            }
        )
    return json.dumps({"MetricDataResults": results})


def response_without_final_rds_cpu_bucket(instance_type: str) -> str:
    document = json.loads(cloudwatch_response(instance_type))
    rds_cpu = next(
        result
        for result in document["MetricDataResults"]
        if result["Id"] == "rds_cpu"
    )
    rds_cpu["Timestamps"].pop()
    rds_cpu["Values"].pop()
    return json.dumps(document)


def response_without_middle_rds_cpu_bucket(instance_type: str) -> str:
    document = json.loads(cloudwatch_response(instance_type))
    rds_cpu = next(
        result
        for result in document["MetricDataResults"]
        if result["Id"] == "rds_cpu"
    )
    rds_cpu["Timestamps"].pop(1)
    rds_cpu["Values"].pop(1)
    return json.dumps(document)


def response_with_overlapping_rds_cpu_bucket(instance_type: str) -> str:
    document = json.loads(cloudwatch_response(instance_type))
    rds_cpu = next(
        result
        for result in document["MetricDataResults"]
        if result["Id"] == "rds_cpu"
    )
    rds_cpu["Timestamps"][1] = "2026-08-29T12:02:30Z"
    return json.dumps(document)


def test_query_uses_native_periods_and_private_resource_dimensions() -> None:
    query = build_cloudwatch_query(
        instance_id=INSTANCE_ID,
        rds_identifier=RDS_IDENTIFIER,
        instance_type="t3.small",
        load_started_at=LOAD_STARTED_AT,
        load_ended_at=LOAD_ENDED_AT,
    )

    assert query["StartTime"] == "2026-08-29T12:00:00+00:00"
    assert query["EndTime"] == "2026-08-29T12:10:00+00:00"
    queries = {item["Id"]: item for item in query["MetricDataQueries"]}
    assert queries["ec2_cpu"]["MetricStat"]["Period"] == 60
    assert queries["rds_write_latency"]["MetricStat"]["Period"] == 60
    assert queries["ec2_cpu_credit_balance"]["MetricStat"]["Period"] == 300
    assert queries["ec2_cpu"]["MetricStat"]["Metric"]["Dimensions"] == [
        {"Name": "InstanceId", "Value": INSTANCE_ID}
    ]
    assert queries["rds_cpu"]["MetricStat"]["Metric"]["Dimensions"] == [
        {"Name": "DBInstanceIdentifier", "Value": RDS_IDENTIFIER}
    ]


def test_non_burstable_tier_does_not_request_credit_metrics() -> None:
    query = build_cloudwatch_query(
        instance_id=INSTANCE_ID,
        rds_identifier=RDS_IDENTIFIER,
        instance_type="c7i-flex.large",
        load_started_at=LOAD_STARTED_AT,
        load_ended_at=LOAD_ENDED_AT,
    )

    query_ids = {item["Id"] for item in query["MetricDataQueries"]}
    assert not any("credit" in query_id for query_id in query_ids)
    assert query["StartTime"] == "2026-08-29T12:02:00+00:00"
    assert query["EndTime"] == "2026-08-29T12:06:00+00:00"


def test_parser_records_exact_bucket_overlap_without_resource_ids() -> None:
    evidence = parse_cloudwatch_response(
        cloudwatch_response("t3.small"),
        test_run_id=TEST_RUN_ID,
        instance_type="t3.small",
        load_started_at=LOAD_STARTED_AT,
        load_ended_at=LOAD_ENDED_AT,
        collected_at=datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
    )

    credit_balance = next(
        series
        for series in evidence.series
        if series.query_id == "ec2_cpu_credit_balance"
    )
    assert credit_balance.period_seconds == 300
    assert tuple(
        point.load_window_overlap_seconds for point in credit_balance.datapoints
    ) == (170, 10)
    ec2_cpu = next(
        series for series in evidence.series if series.query_id == "ec2_cpu"
    )
    assert tuple(
        point.load_window_overlap_seconds for point in ec2_cpu.datapoints
    ) == (50, 60, 60, 10)
    serialized = evidence.model_dump_json()
    assert INSTANCE_ID not in serialized
    assert RDS_IDENTIFIER not in serialized


def test_collection_retries_until_every_metric_is_published(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    responses = [json.dumps({"MetricDataResults": []}), cloudwatch_response("t3.small")]
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        call = tuple(arguments)
        calls.append(call)
        query_path = Path(call[call.index("--cli-input-json") + 1].removeprefix("file://"))
        query = json.loads(query_path.read_text(encoding="utf-8"))
        assert len(query["MetricDataQueries"]) == 12
        return completed(call, stdout=responses.pop(0))

    evidence = collect_cloudwatch_evidence(
        session,
        instance_id=INSTANCE_ID,
        rds_identifier=RDS_IDENTIFIER,
        test_run_id=TEST_RUN_ID,
        load_started_at=LOAD_STARTED_AT,
        load_ended_at=LOAD_ENDED_AT,
        runner=runner,
        sleeper=sleeps.append,
        now=lambda: datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        maximum_attempts=2,
        retry_interval_seconds=1,
    )

    assert str(evidence.test_run_id) == TEST_RUN_ID
    assert len(calls) == 2
    assert sleeps == [1]
    assert calls[0][:6] == (
        "aws",
        "--profile",
        "trackrelay-admin",
        "--region",
        "ap-southeast-3",
        "cloudwatch",
    )


def test_collection_waits_for_a_delayed_trailing_metric_bucket(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    responses = [
        response_without_final_rds_cpu_bucket("t3.small"),
        cloudwatch_response("t3.small"),
    ]
    sleeps: list[float] = []

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        return completed(tuple(arguments), stdout=responses.pop(0))

    evidence = collect_cloudwatch_evidence(
        session,
        instance_id=INSTANCE_ID,
        rds_identifier=RDS_IDENTIFIER,
        test_run_id=TEST_RUN_ID,
        load_started_at=LOAD_STARTED_AT,
        load_ended_at=LOAD_ENDED_AT,
        runner=runner,
        sleeper=sleeps.append,
        now=lambda: datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        maximum_attempts=2,
        retry_interval_seconds=1,
    )

    assert str(evidence.test_run_id) == TEST_RUN_ID
    assert responses == []
    assert sleeps == [1]


def test_parser_reports_the_exact_unpublished_tail() -> None:
    with raises(
        AwsRehostError,
        match=(
            "rds_cpu\\[2026-08-29T12:05:00\\+00:00"
            "\\.\\.2026-08-29T12:05:10\\+00:00\\]"
        ),
    ):
        parse_cloudwatch_response(
            response_without_final_rds_cpu_bucket("t3.small"),
            test_run_id=TEST_RUN_ID,
            instance_type="t3.small",
            load_started_at=LOAD_STARTED_AT,
            load_ended_at=LOAD_ENDED_AT,
            collected_at=datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        )


def test_parser_rejects_an_interior_metric_gap() -> None:
    with raises(
        AwsRehostError,
        match=(
            "rds_cpu\\[2026-08-29T12:03:00\\+00:00"
            "\\.\\.2026-08-29T12:04:00\\+00:00\\]"
        ),
    ):
        parse_cloudwatch_response(
            response_without_middle_rds_cpu_bucket("t3.small"),
            test_run_id=TEST_RUN_ID,
            instance_type="t3.small",
            load_started_at=LOAD_STARTED_AT,
            load_ended_at=LOAD_ENDED_AT,
            collected_at=datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        )


def test_parser_rejects_overlapping_native_buckets() -> None:
    with raises(
        AwsRehostError,
        match="overlapping native buckets: rds_cpu@2026-08-29T12:02:30\\+00:00",
    ):
        parse_cloudwatch_response(
            response_with_overlapping_rds_cpu_bucket("t3.small"),
            test_run_id=TEST_RUN_ID,
            instance_type="t3.small",
            load_started_at=LOAD_STARTED_AT,
            load_ended_at=LOAD_ENDED_AT,
            collected_at=datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        )


def test_collection_timeout_preserves_the_last_coverage_diagnostic(
    tmp_path: Path,
) -> None:
    session = make_session(tmp_path)
    partial_response = response_without_final_rds_cpu_bucket("t3.small")

    def runner(
        arguments: Sequence[str],
        input_text: str | None,
    ) -> CompletedProcess[str]:
        del input_text
        return completed(tuple(arguments), stdout=partial_response)

    with raises(
        AwsRehostError,
        match="last observation:.*rds_cpu",
    ):
        collect_cloudwatch_evidence(
            session,
            instance_id=INSTANCE_ID,
            rds_identifier=RDS_IDENTIFIER,
            test_run_id=TEST_RUN_ID,
            load_started_at=LOAD_STARTED_AT,
            load_ended_at=LOAD_ENDED_AT,
            runner=runner,
            sleeper=lambda _: None,
            maximum_attempts=2,
            retry_interval_seconds=1,
        )


def test_incomplete_cloudwatch_evidence_is_rejected() -> None:
    with raises(AwsRehostError, match="incomplete"):
        parse_cloudwatch_response(
            json.dumps({"MetricDataResults": []}),
            test_run_id=TEST_RUN_ID,
            instance_type="t3.small",
            load_started_at=LOAD_STARTED_AT,
            load_ended_at=LOAD_ENDED_AT,
            collected_at=datetime(2026, 8, 29, 12, 8, tzinfo=UTC),
        )
