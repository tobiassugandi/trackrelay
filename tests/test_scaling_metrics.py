"""The scaling signal is global, retry-aware, and independent of ingestion."""

import asyncio
from datetime import timedelta
from unittest.mock import Mock

from tests.services.test_delivery_attempt_recording import (
    STARTED_AT,
    create_event_fixture,
)
from trackrelay import scaling_metrics
from trackrelay.config import Settings
from trackrelay.domain import DeliveryAttemptResult
from trackrelay.models import DeliveryAttempt, Event


def test_snapshot_counts_unique_arrivals_and_successful_retries():
    engine, sessions, _, event_id = create_event_fixture()
    now = STARTED_AT + timedelta(seconds=10)
    with sessions.begin() as session:
        session.get(Event, event_id).created_at = now - timedelta(seconds=2)
    with sessions() as session:
        assert scaling_metrics.demand_snapshot(session, now) == (0.1, 1)
    with sessions.begin() as session:
        for attempt in (1, 2):
            session.add(
                DeliveryAttempt(
                    event_id=event_id,
                    attempt_number=attempt,
                    result=DeliveryAttemptResult.DELIVERED,
                    response_code=202,
                    latency_ms=1,
                    started_at=now,
                    completed_at=now,
                )
            )
    with sessions() as session:
        assert scaling_metrics.demand_snapshot(session, now) == (0.1, 0)
        assert scaling_metrics.demand_snapshot(
            session, now + timedelta(seconds=11)
        ) == (0, 0)
    engine.dispose()


def test_snapshot_half_open_window_and_empty_database():
    engine, sessions, _, event_id = create_event_fixture()
    now = STARTED_AT + timedelta(seconds=10)
    for created, rate in (
        (now, 0),
        (now - timedelta(seconds=10), 0.1),
        (now - timedelta(seconds=11), 0),
    ):
        with sessions.begin() as session:
            session.get(Event, event_id).created_at = created
        with sessions() as session:
            assert scaling_metrics.demand_snapshot(session, now) == (rate, 1)
    with sessions.begin() as session:
        session.delete(session.get(Event, event_id))
    with sessions() as session:
        assert scaling_metrics.demand_snapshot(session, now) == (0, 0)
    engine.dispose()


def test_publication_is_high_resolution_global_gauges_including_zero():
    client = Mock()
    scaling_metrics.publish_snapshot(client, "test-delivery", STARTED_AT, (0, 0))
    request = client.put_metric_data.call_args.kwargs
    assert request["Namespace"] == "TrackRelay/Elasticity"
    assert [m["MetricName"] for m in request["MetricData"]] == [
        "ArrivalRate",
        "OutstandingEvents",
    ]
    for metric in request["MetricData"]:
        assert metric["StorageResolution"] == 1
        assert metric["Value"] == 0
        assert metric["Timestamp"] == STARTED_AT
        assert metric["Dimensions"] == [{"Name": "QueueName", "Value": "test-delivery"}]


def test_failed_sample_does_not_publish_zero_and_loop_recovers(monkeypatch):
    stop = Mock()
    stop.wait.side_effect = [False, False, True]
    session_factory = Mock()
    session_factory.return_value.__enter__ = Mock()
    session_factory.return_value.__exit__ = Mock(return_value=False)
    snapshot = Mock(side_effect=[RuntimeError("database unavailable"), (5, 30)])
    monkeypatch.setattr(scaling_metrics, "demand_snapshot", snapshot)
    client = Mock()
    scaling_metrics.run_publisher(stop, session_factory, client, "test-delivery")
    assert snapshot.call_count == 2
    assert client.put_metric_data.call_count == 1
    assert client.put_metric_data.call_args.kwargs["MetricData"][0]["Value"] == 5


def test_lifespan_disabled_by_default_without_aws_calls(monkeypatch):
    monkeypatch.setattr(scaling_metrics, "Settings", lambda: Settings(_env_file=None))
    client = Mock(side_effect=AssertionError("must not access AWS"))
    monkeypatch.setattr(scaling_metrics.boto3, "client", client)

    async def run():
        async with scaling_metrics.scaling_metrics_lifespan(None):
            pass

    asyncio.run(run())
    client.assert_not_called()


def test_lifespan_stops_and_closes_resources(monkeypatch):
    from threading import Event as StopEvent

    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        scaling_metrics_queue_name="test-delivery",
    )
    monkeypatch.setattr(scaling_metrics, "Settings", lambda: settings)
    engine = Mock()
    monkeypatch.setattr(scaling_metrics, "create_database_engine", lambda _: engine)
    client = Mock()
    monkeypatch.setattr(scaling_metrics.boto3, "client", lambda *a, **kw: client)
    stopped = StopEvent()

    def run_publisher(stop, *args):
        stop.wait()
        stopped.set()

    monkeypatch.setattr(scaling_metrics, "run_publisher", run_publisher)

    async def run():
        async with scaling_metrics.scaling_metrics_lifespan(None):
            pass

    asyncio.run(run())
    assert stopped.is_set()
    client.close.assert_called_once()
    engine.dispose.assert_called_once()
