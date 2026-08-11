"""TrackRelay API application."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trackrelay.config import Settings
from trackrelay.database import check_database_connection, get_session
from trackrelay.domain import NormalizedEvent
from trackrelay.models import Partner
from trackrelay.partners import CourierAlphaAdapter, CourierAlphaPayload
from trackrelay.services import persist_normalized_event

settings = Settings()
app = FastAPI(title=settings.app_name, debug=settings.debug)

EventPersister = Callable[[NormalizedEvent], UUID]


def get_event_persister() -> EventPersister:
    """Provide the application service used to persist normalized events."""
    return persist_normalized_event


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


@app.post(
    "/api/v1/partners/{partner_id}/events",
    status_code=status.HTTP_201_CREATED,
    tags=["events"],
)
def ingest_partner_event(
    partner_id: str,
    payload: CourierAlphaPayload,
    session: Annotated[Session, Depends(get_session)],
    persist_event: Annotated[EventPersister, Depends(get_event_persister)],
) -> dict[str, str]:
    """Validate, normalize, and persist one Courier Alpha event."""
    partner = session.get(Partner, partner_id)
    if partner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Partner not found",
        )
    if not partner.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Partner is inactive",
        )
    if partner.adapter_type != CourierAlphaAdapter.partner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Partner adapter is not supported",
        )

    normalized_event = CourierAlphaAdapter().normalize(
        payload,
        received_at=datetime.now(UTC),
    )
    event_id = persist_event(normalized_event)
    return {"event_id": str(event_id), "status": "processed"}
