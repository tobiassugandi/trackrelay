"""Endpoint timing never records payloads or identifiers from raw paths."""

import asyncio
from json import loads
from types import SimpleNamespace

from pytest import mark

from trackrelay.request_timing import RequestTimingMiddleware, drain_ingestion_metrics


def test_request_timing_uses_route_template_and_status(capsys):
    drain_ingestion_metrics("test-delivery")

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v1/partners/{partner_id}/events")
        await send({"type": "http.response.start", "status": 201})
        await send({"type": "http.response.body", "body": b"private payload"})

    async def send(message):
        pass

    asyncio.run(
        RequestTimingMiddleware(app, enabled=True)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/partners/private-id/events",
            },
            None,
            send,
        )
    )
    output = capsys.readouterr().out
    item = loads(output)
    assert item["route"] == "/api/v1/partners/{partner_id}/events"
    assert item["status"] == 201 and item["duration_ms"] >= 0
    assert "private" not in output
    samples = drain_ingestion_metrics("test-delivery")
    assert len(samples) == 1
    assert samples[0]["MetricName"] == "ServerIngestionLatency"
    assert samples[0]["StorageResolution"] == 1
    assert samples[0]["Values"] == [item["duration_ms"]]


def test_disabled_timing_does_not_log(capsys):
    async def app(*args):
        pass

    asyncio.run(RequestTimingMiddleware(app)({"type": "http"}, None, None))
    assert not capsys.readouterr().out


@mark.parametrize("failed_stage", (None, "persistence", "publication"))
def test_real_sync_ingestion_stages_cross_threadpool_and_keep_errors_private(
    capsys, failed_stage
):
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from tests.test_ingestion import configure_dependencies, configured_partner
    from trackrelay.main import app, get_delivery_outbox_publisher
    from trackrelay.services import DownstreamDeliveryQueueError, EventPersistenceResult

    def persist(event):
        if failed_stage == "persistence":
            raise RuntimeError("private persistence details")
        return EventPersistenceResult(event_id=uuid4(), duplicate=False)

    def publish(*args):
        if failed_stage == "publication":
            raise DownstreamDeliveryQueueError("private queue details")
        return True

    configure_dependencies(configured_partner(), persist)
    app.dependency_overrides[get_delivery_outbox_publisher] = lambda: publish
    try:
        with TestClient(
            RequestTimingMiddleware(app, enabled=True), raise_server_exceptions=False
        ) as client:
            response = client.post(
                "/api/v1/partners/courier-alpha/events",
                json={
                    "event_id": "private-event",
                    "tracking_number": "private-shipment",
                    "status": "PICKUP",
                    "event_time": "2026-08-06T10:00:00+07:00",
                },
            )
        assert response.status_code == (500 if failed_stage == "persistence" else 201)
        record = loads(capsys.readouterr().out)
        stages = record["stages"]
        assert [s["stage"] for s in stages] == (
            ["partner_lookup", "persistence"]
            if failed_stage == "persistence"
            else ["partner_lookup", "persistence", "publication"]
        )
        assert all(
            s["outcome"] == ("error" if s["stage"] == failed_stage else "ok")
            for s in stages
        )
        assert all(s["duration_ms"] >= 0 and s["start_ms"] >= 0 for s in stages)
        assert record["unattributed_ms"] >= 0
        assert "private" not in str(record) and "courier-alpha" not in str(record)
    finally:
        app.dependency_overrides.clear()
        drain_ingestion_metrics("test-delivery")


def test_concurrent_requests_do_not_mix_threadpool_stage_traces(capsys):
    from trackrelay.request_timing import _trace, ingestion_stage

    async def app(scope, receive, send):
        def work():
            with ingestion_stage(scope["stage"]):
                pass

        await asyncio.to_thread(work)
        await send({"type": "http.response.start", "status": 201})

    async def send(message):
        pass

    async def run():
        middleware = RequestTimingMiddleware(app, enabled=True)
        await asyncio.gather(
            *(
                middleware({"type": "http", "stage": stage}, None, send)
                for stage in ("persistence", "publication")
            )
        )
        assert _trace.get() is None

    asyncio.run(run())
    records = [loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len({record["trace_id"] for record in records}) == 2
    assert sorted(record["stages"][0]["stage"] for record in records) == [
        "persistence",
        "publication",
    ]
    assert all(len(record["stages"]) == 1 for record in records)
