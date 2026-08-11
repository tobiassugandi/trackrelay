"""Downstream order-system simulator API."""

from fastapi import FastAPI, status

from trackrelay.domain import NormalizedEvent
from trackrelay.downstream.store import DownstreamEventStore

app = FastAPI(title="TrackRelay Downstream Simulator")
event_store = DownstreamEventStore()


@app.post("/events", status_code=status.HTTP_202_ACCEPTED)
def receive_event(event: NormalizedEvent) -> dict[str, object]:
    """Accept and record one normalized event."""
    received_count = event_store.record(event)
    return {"status": "accepted", "received_count": received_count}


@app.get("/events")
def list_events() -> list[NormalizedEvent]:
    """Return every event received by this simulator process."""
    return list(event_store.all())
