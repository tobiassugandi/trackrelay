"""Retain scaling signal and action timing without changing native guardrails."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from json import dumps, loads
from pathlib import Path

from trackrelay.aws_async_deployment import ProcessRunner, aws_prefix, invoke
from trackrelay.aws_session import AwsSession
from trackrelay.operator_status import operator_status


def collect_scaling_diagnostics(
    session: AwsSession,
    dimensions: Mapping[str, str],
    start: datetime,
    end: datetime,
    root: Path,
    *,
    runner: ProcessRunner,
) -> None:
    """Best-effort auxiliary evidence; collection errors cannot prevent teardown."""
    root.mkdir(parents=True, exist_ok=True)
    queries = [
        {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": "TrackRelay/Elasticity",
                    "MetricName": metric,
                    "Dimensions": [
                        {
                            "Name": "QueueName",
                            "Value": dimensions["delivery_queue_name"],
                        }
                    ],
                },
                "Period": 10,
                "Stat": "Maximum",
            },
            "ReturnData": True,
        }
        for query_id, metric in (
            ("arrival_rate", "ArrivalRate"),
            ("outstanding", "OutstandingEvents"),
        )
    ]
    window = (
        "--start-time",
        (start - timedelta(minutes=5)).isoformat(),
        "--end-time",
        end.isoformat(),
    )
    commands = {
        "high-resolution-metrics": (
            "cloudwatch",
            "get-metric-data",
            "--metric-data-queries",
            dumps(queries),
            *window,
            "--scan-by",
            "TimestampAscending",
        ),
        "scaling-activities": (
            "application-autoscaling",
            "describe-scaling-activities",
            "--service-namespace",
            "ecs",
            "--scalable-dimension",
            "ecs:service:DesiredCount",
            "--resource-id",
            f"service/{dimensions['cluster_name']}/{dimensions['worker_service_name']}",
            "--include-not-scaled-activities",
        ),
        **{
            f"alarm-{suffix}": (
                "cloudwatch",
                "describe-alarm-history",
                "--alarm-name",
                f"{dimensions['worker_service_name']}-{suffix}",
                "--start-date",
                (start - timedelta(minutes=5)).isoformat(),
                "--end-date",
                end.isoformat(),
            )
            for suffix in ("demand-high", "release-safe")
        },
    }
    failures = {}
    for name, command in commands.items():
        try:
            response = invoke(
                runner,
                (*aws_prefix(session), *command, "--output", "json"),
                action=f"Scaling diagnostics: {name}",
            )
            document = loads(response.stdout)
            (root / f"{name}.json").write_text(
                dumps(document, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as error:  # noqa: BLE001 - preserve primary outcome and cleanup
            failures[name] = type(error).__name__
    (root / "collection.json").write_text(
        dumps(
            {"schema_version": 1, "complete": not failures, "failures": failures},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    operator_status(
        "Scaling timing diagnostics retained"
        if not failures
        else "Scaling timing diagnostics incomplete; see collection.json"
    )
