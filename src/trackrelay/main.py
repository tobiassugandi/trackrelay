"""TrackRelay API application."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trackrelay.config import Settings
from trackrelay.database import check_database_connection, get_session
from trackrelay.domain import NormalizedEvent
from trackrelay.models import Partner
from trackrelay.partners import CourierAlphaAdapter, CourierAlphaPayload
from trackrelay.services import (
    DeliveryResult,
    EventPersistenceResult,
    deliver_normalized_event,
    persist_normalized_event,
)

settings = Settings()
app = FastAPI(title=settings.app_name, debug=settings.debug)

EventPersister = Callable[[NormalizedEvent], EventPersistenceResult]
EventDeliverer = Callable[[NormalizedEvent], DeliveryResult]


class CreatedEventResponse(BaseModel):
    """Outcome when a logical event is created and delivered."""

    event_id: UUID
    processing_status: Literal["processed"]
    duplicate: Literal[False]
    delivery_status: Literal["delivered"]
    downstream_status_code: int


class DuplicateEventResponse(BaseModel):
    """Contract for a retry that resolves to an existing logical event."""

    event_id: UUID
    processing_status: Literal["processed"]
    duplicate: Literal[True]
    delivery_status: Literal["skipped_duplicate"]
    downstream_status_code: None


IngestionResponse = CreatedEventResponse | DuplicateEventResponse


def get_event_persister() -> EventPersister:
    """Provide the application service used to persist normalized events."""
    return persist_normalized_event


def get_event_deliverer() -> EventDeliverer:
    """Provide synchronous delivery configured for the local downstream service."""

    def deliver(event: NormalizedEvent) -> DeliveryResult:
        return deliver_normalized_event(
            event,
            downstream_url=settings.downstream_url,
            timeout_seconds=settings.downstream_timeout_seconds,
        )

    return deliver


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
    response_model=IngestionResponse,
    tags=["events"],
)
def ingest_partner_event(
    partner_id: str,
    payload: CourierAlphaPayload,
    session: Annotated[Session, Depends(get_session)],
    persist_event: Annotated[EventPersister, Depends(get_event_persister)],
    deliver_event: Annotated[EventDeliverer, Depends(get_event_deliverer)],
) -> CreatedEventResponse:
    """Validate, normalize, persist, and deliver one Courier Alpha event."""
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
    persistence = persist_event(normalized_event)
    delivery = deliver_event(normalized_event)
    return CreatedEventResponse(
        event_id=persistence.event_id,
        processing_status="processed",
        duplicate=False,
        delivery_status=delivery.status,
        downstream_status_code=delivery.downstream_status_code,
    )
