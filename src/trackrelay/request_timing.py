"""Opt-in structured endpoint timing; never log payloads or raw URL identities."""

from collections import defaultdict, deque
from datetime import UTC, datetime
from json import dumps
from threading import Lock
from time import perf_counter

_samples = deque(maxlen=5000)
_lock = Lock()


def drain_ingestion_metrics(queue_name):
    with _lock:
        samples = tuple(_samples)
        _samples.clear()
    groups = defaultdict(list)
    for at, duration in samples:
        groups[at.replace(microsecond=0)].append(duration)
    # Bound MetricData as well as raw values. A long publication outage at 1/s
    # can leave thousands of distinct seconds; keep the newest diagnostics and
    # leave room for the five control gauges in one PutMetricData request.
    return [
        {
            "MetricName": "ServerIngestionLatency",
            "Timestamp": at,
            "Values": values[offset : offset + 150],
            "Unit": "Milliseconds",
            "StorageResolution": 1,
            "Dimensions": [{"Name": "QueueName", "Value": queue_name}],
        }
        for at, values in groups.items()
        for offset in range(0, len(values), 150)
    ][-900:]


class RequestTimingMiddleware:
    def __init__(self, app, *, enabled: bool = False):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        status = 500

        async def timed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            at = datetime.now(UTC)
            duration = (perf_counter() - started) * 1000
            if route == "/api/v1/partners/{partner_id}/events":
                with _lock:
                    _samples.append((at, duration))
            print(
                dumps(
                    {
                        "kind": "request_timing",
                        "at": at.isoformat(),
                        "method": scope.get("method"),
                        "route": route,
                        "status": status,
                        "duration_ms": duration,
                    }
                ),
                flush=True,
            )
