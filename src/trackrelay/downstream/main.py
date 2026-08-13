"""Downstream order-system simulator API."""

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

from trackrelay.domain import NormalizedEvent
from trackrelay.downstream.control import SimulatorControl, SimulatorMode
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


@app.put("/control/mode", response_model=SimulatorStatusResponse)
def set_simulator_mode(request: SimulatorModeRequest) -> SimulatorStatusResponse:
    """Activate one deterministic downstream behavior."""
    mode = simulator_control.set_mode(request.mode)
    return SimulatorStatusResponse(mode=mode)


@app.get("/control/status", response_model=SimulatorStatusResponse)
def get_simulator_status() -> SimulatorStatusResponse:
    """Report the currently active downstream behavior."""
    return SimulatorStatusResponse(mode=simulator_control.get_mode())


@app.post("/events", status_code=status.HTTP_202_ACCEPTED)
def receive_event(event: NormalizedEvent) -> dict[str, object]:
    """Accept and record one normalized event."""
    if simulator_control.get_mode() is SimulatorMode.RETURN_500:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Simulated downstream failure",
        )

    received_count = event_store.record(event)
    return {"status": "accepted", "received_count": received_count}


@app.get("/events")
def list_events() -> list[NormalizedEvent]:
    """Return every event received by this simulator process."""
    return list(event_store.all())
