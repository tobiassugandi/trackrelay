"""TrackRelay API application."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trackrelay.config import Settings
from trackrelay.database import check_database_connection, get_session
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.models import DeliveryAttempt, Event, Partner, Shipment
from trackrelay.partners import CourierAlphaAdapter, CourierAlphaPayload
from trackrelay.services import (
    DeliveryResult,
    EventPersistenceResult,
    deliver_and_record_normalized_event,
    list_shipment_events,
    persist_normalized_event,
)

settings = Settings()
app = FastAPI(title=settings.app_name, debug=settings.debug)

EventPersister = Callable[[NormalizedEvent], EventPersistenceResult]
EventDeliverer = Callable[[NormalizedEvent, UUID], DeliveryResult]


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


class ShipmentHistoryEventResponse(BaseModel):
    """One applied or rejected event in a shipment's audit history."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    partner_id: str
    partner_event_id: str
    tracking_number: str
    status: ShipmentStatus
    occurred_at: datetime
    received_at: datetime
    raw_payload: dict[str, JsonValue]
    processing_status: EventProcessingStatus
    state_applied: bool
    state_rejection_reason: str | None
    created_at: datetime


class ShipmentResponse(BaseModel):
    """The latest accepted state for one tracked shipment."""

    model_config = ConfigDict(from_attributes=True)

    tracking_number: str
    current_status: ShipmentStatus
    current_status_occurred_at: datetime
    created_at: datetime
    updated_at: datetime


class DeliveryAttemptResponse(BaseModel):
    """One observable downstream delivery attempt."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    attempt_number: int
    result: DeliveryAttemptResult
    response_code: int | None
    latency_ms: int
    error: str | None
    started_at: datetime
    completed_at: datetime


class EventInspectionResponse(ShipmentHistoryEventResponse):
    """A persisted event with its downstream delivery diagnostics."""

    delivery_attempts: list[DeliveryAttemptResponse]


def get_event_persister() -> EventPersister:
    """Provide the application service used to persist normalized events."""
    return persist_normalized_event


def get_event_deliverer() -> EventDeliverer:
    """Provide synchronous delivery configured for the local downstream service."""

    def deliver(event: NormalizedEvent, event_id: UUID) -> DeliveryResult:
        return deliver_and_record_normalized_event(
            event,
            event_id=event_id,
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


@app.get(
    "/api/v1/shipments/{tracking_number}",
    response_model=ShipmentResponse,
    tags=["shipments"],
)
def get_shipment(
    tracking_number: str,
    session: Annotated[Session, Depends(get_session)],
) -> Shipment:
    """Return the latest accepted state for one shipment."""
    shipment = session.get(Shipment, tracking_number)
    if shipment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipment not found",
        )
    return shipment


@app.get(
    "/api/v1/shipments/{tracking_number}/events",
    response_model=list[ShipmentHistoryEventResponse],
    tags=["shipments"],
)
def shipment_history(
    tracking_number: str,
    session: Annotated[Session, Depends(get_session)],
) -> tuple[Event, ...]:
    """Return applied and rejected events in deterministic business-time order."""
    events = list_shipment_events(tracking_number, session=session)
    if events is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipment not found",
        )
    return events


@app.get(
    "/api/v1/events/{event_id}",
    response_model=EventInspectionResponse,
    tags=["events"],
)
def get_event(
    event_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> EventInspectionResponse:
    """Return one persisted event and its ordered delivery attempts."""
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    attempts = list(
        session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.event_id == event_id)
            .order_by(DeliveryAttempt.attempt_number.asc())
        )
    )
    return EventInspectionResponse(
        id=event.id,
        partner_id=event.partner_id,
        partner_event_id=event.partner_event_id,
        tracking_number=event.tracking_number,
        status=event.status,
        occurred_at=event.occurred_at,
        received_at=event.received_at,
        raw_payload=event.raw_payload,
        processing_status=event.processing_status,
        state_applied=event.state_applied,
        state_rejection_reason=event.state_rejection_reason,
        created_at=event.created_at,
        delivery_attempts=attempts,
    )


@app.post(
    "/api/v1/partners/{partner_id}/events",
    status_code=status.HTTP_201_CREATED,
    response_model=IngestionResponse,
    responses={
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Event persisted, but downstream delivery failed."
        },
        status.HTTP_504_GATEWAY_TIMEOUT: {
            "description": "Event persisted, but downstream delivery timed out."
        },
    },
    tags=["events"],
)
def ingest_partner_event(
    partner_id: str,
    payload: CourierAlphaPayload,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    persist_event: Annotated[EventPersister, Depends(get_event_persister)],
    deliver_event: Annotated[EventDeliverer, Depends(get_event_deliverer)],
) -> IngestionResponse:
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
    if persistence.duplicate:
        response.status_code = status.HTTP_200_OK
        return DuplicateEventResponse(
            event_id=persistence.event_id,
            processing_status="processed",
            duplicate=True,
            delivery_status="skipped_duplicate",
            downstream_status_code=None,
        )

    try:
        delivery = deliver_event(normalized_event, persistence.event_id)
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Downstream delivery timed out; event remains persisted",
        ) from error
    except (httpx.HTTPStatusError, httpx.TransportError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Downstream delivery failed; event remains persisted",
        ) from error

    return CreatedEventResponse(
        event_id=persistence.event_id,
        processing_status="processed",
        duplicate=False,
        delivery_status=delivery.status,
        downstream_status_code=delivery.downstream_status_code,
    )
