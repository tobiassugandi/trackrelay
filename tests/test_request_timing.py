"""Endpoint timing never records payloads or identifiers from raw paths."""

import asyncio
from json import loads
from types import SimpleNamespace

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
