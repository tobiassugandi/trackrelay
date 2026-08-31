"""Downstream order-system simulator API."""

from time import sleep
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from trackrelay.domain import NormalizedEvent
from trackrelay.downstream.control import (
    SimulatorControl,
    SimulatorMode,
    simulator_delay_seconds,
)
from trackrelay.downstream.store import DownstreamEventStore
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot, capture_runtime_metrics

app = FastAPI(title="TrackRelay Downstream Simulator")
event_store = DownstreamEventStore()
simulator_control = SimulatorControl()


class SimulatorModeRequest(BaseModel):
    """Validated request to change simulator behavior."""

    model_config = ConfigDict(extra="forbid")

    mode: SimulatorMode


class SimulatorStatusResponse(BaseModel):
    """Current simulator control state."""

    mode: SimulatorMode
    delay_seconds: float


class SimulatorResetResponse(BaseModel):
    """Receipt count removed between isolated experiment treatments."""

    cleared_event_count: int


def simulator_status(mode: SimulatorMode) -> SimulatorStatusResponse:
    """Describe one mode including its deterministic delay."""
    return SimulatorStatusResponse(
        mode=mode,
        delay_seconds=simulator_delay_seconds(mode),
    )


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    """Report that the downstream simulator process is running."""
    return {"status": "ok"}


@app.get(
    "/experiments/runtime-metrics",
    response_model=RuntimeMetricsSnapshot,
    tags=["experiments"],
)
def get_runtime_metrics() -> RuntimeMetricsSnapshot:
    """Expose simulator-process measurements without a database-pool claim."""
    return capture_runtime_metrics(database_engine=None)


@app.put("/control/mode", response_model=SimulatorStatusResponse)
def set_simulator_mode(request: SimulatorModeRequest) -> SimulatorStatusResponse:
    """Activate one deterministic downstream behavior."""
    mode = simulator_control.set_mode(request.mode)
    return simulator_status(mode)


@app.get("/control/status", response_model=SimulatorStatusResponse)
def get_simulator_status() -> SimulatorStatusResponse:
    """Report the currently active downstream behavior."""
    return simulator_status(simulator_control.get_mode())


@app.delete("/control/events", response_model=SimulatorResetResponse)
def clear_simulator_events() -> SimulatorResetResponse:
    """Clear private synthetic receipts between experiment treatments."""
    return SimulatorResetResponse(
        cleared_event_count=event_store.clear(),
    )


@app.post("/events", status_code=status.HTTP_202_ACCEPTED)
def receive_event(
    event: NormalizedEvent,
    idempotency_key: Annotated[
        UUID | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> dict[str, object]:
    """Accept and record one normalized event."""
    mode = simulator_control.get_mode()
    if mode is SimulatorMode.RETURN_500:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Simulated downstream failure",
        )
    if mode is SimulatorMode.UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulated downstream unavailability",
        )

    delay_seconds = simulator_delay_seconds(mode)
    if delay_seconds:
        sleep(delay_seconds)

    result = event_store.record(event, idempotency_key=idempotency_key)
    return {
        "status": "accepted",
        "received_count": result.received_count,
        "duplicate": result.duplicate,
    }


@app.get("/events", response_model_exclude_none=True)
def list_events(test_run_id: UUID | None = None) -> list[NormalizedEvent]:
    """Return all receipts, optionally scoped to one experiment."""
    events = event_store.all()
    if test_run_id is None:
        return list(events)
    return [event for event in events if event.test_run_id == test_run_id]
