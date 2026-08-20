"""Downstream order-system simulator API."""

from time import sleep
from uuid import UUID

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

from trackrelay.domain import NormalizedEvent
from trackrelay.downstream.control import (
    SimulatorControl,
    SimulatorMode,
    simulator_delay_seconds,
)
from trackrelay.downstream.store import DownstreamEventStore

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


def simulator_status(mode: SimulatorMode) -> SimulatorStatusResponse:
    """Describe one mode including its deterministic delay."""
    return SimulatorStatusResponse(
        mode=mode,
        delay_seconds=simulator_delay_seconds(mode),
    )


@app.put("/control/mode", response_model=SimulatorStatusResponse)
def set_simulator_mode(request: SimulatorModeRequest) -> SimulatorStatusResponse:
    """Activate one deterministic downstream behavior."""
    mode = simulator_control.set_mode(request.mode)
    return simulator_status(mode)


@app.get("/control/status", response_model=SimulatorStatusResponse)
def get_simulator_status() -> SimulatorStatusResponse:
    """Report the currently active downstream behavior."""
    return simulator_status(simulator_control.get_mode())


@app.post("/events", status_code=status.HTTP_202_ACCEPTED)
def receive_event(event: NormalizedEvent) -> dict[str, object]:
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

    received_count = event_store.record(event)
    return {"status": "accepted", "received_count": received_count}


@app.get("/events", response_model_exclude_none=True)
def list_events(test_run_id: UUID | None = None) -> list[NormalizedEvent]:
    """Return all receipts, optionally scoped to one experiment."""
    events = event_store.all()
    if test_run_id is None:
        return list(events)
    return [event for event in events if event.test_run_id == test_run_id]
