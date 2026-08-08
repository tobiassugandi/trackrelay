"""TrackRelay API application."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from trackrelay.config import Settings
from trackrelay.database import check_database_connection

settings = Settings()
app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    """Report that the API process is running."""
    return {"status": "ok"}


def database_is_ready() -> bool:
    """Report whether the API can communicate with its database."""
    try:
        return check_database_connection()
    except SQLAlchemyError:
        return False


@app.get("/health/ready", tags=["health"])
def readiness(
    database_ready: Annotated[bool, Depends(database_is_ready)],
) -> dict[str, str]:
    """Report whether the API is ready to serve database-backed requests."""
    if not database_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
    return {"status": "ready"}
