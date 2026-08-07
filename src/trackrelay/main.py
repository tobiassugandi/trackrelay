"""TrackRelay API application."""

from fastapi import FastAPI

app = FastAPI(title="TrackRelay")


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    """Report that the API process is running."""
    return {"status": "ok"}
