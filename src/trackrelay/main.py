"""TrackRelay API application."""

from fastapi import FastAPI

from trackrelay.config import Settings

settings = Settings()
app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    """Report that the API process is running."""
    return {"status": "ok"}
