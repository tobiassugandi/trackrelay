"""Synchronous delivery to the downstream order system."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trackrelay.database import session_factory
from trackrelay.domain import DeliveryAttemptResult, NormalizedEvent
from trackrelay.models import DeliveryAttempt


@dataclass(frozen=True)
class DeliveryResult:
    """The smallest useful record of one successful HTTP delivery."""

    downstream_status_code: int
    status: Literal["delivered"] = "delivered"


def record_delivery_attempt(
    *,
    event_id: UUID,
    result: DeliveryAttemptResult,
    response_code: int | None,
    latency_ms: int,
    error: str | None,
    started_at: datetime,
    completed_at: datetime,
    sessions: sessionmaker[Session] = session_factory,
) -> UUID:
    """Persist one numbered downstream attempt in its own transaction."""
    with sessions.begin() as session:
        previous_attempt_number = session.scalar(
            select(func.max(DeliveryAttempt.attempt_number))
            .where(DeliveryAttempt.event_id == event_id)
        )
        attempt = DeliveryAttempt(
            event_id=event_id,
            attempt_number=(previous_attempt_number or 0) + 1,
            result=result,
            response_code=response_code,
            latency_ms=latency_ms,
            error=error,
            started_at=started_at,
            completed_at=completed_at,
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id

    return attempt_id


def deliver_normalized_event(
    normalized_event: NormalizedEvent,
    *,
    downstream_url: str,
    timeout_seconds: float = 5.0,
    client: httpx.Client | None = None,
) -> DeliveryResult:
    """POST one normalized event and return its successful delivery result."""
    endpoint = f"{downstream_url.rstrip('/')}/events"
    body = normalized_event.model_dump(mode="json")

    if client is not None:
        response = client.post(endpoint, json=body)
    else:
        with httpx.Client(timeout=timeout_seconds) as http_client:
            response = http_client.post(endpoint, json=body)

    response.raise_for_status()
    return DeliveryResult(downstream_status_code=response.status_code)


def deliver_and_record_normalized_event(
    normalized_event: NormalizedEvent,
    *,
    event_id: UUID,
    downstream_url: str,
    timeout_seconds: float = 5.0,
    client: httpx.Client | None = None,
    sessions: sessionmaker[Session] = session_factory,
    monotonic: Callable[[], float] = perf_counter,
    utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DeliveryResult:
    """Deliver an event and durably record the transport outcome."""
    started_at = utcnow()
    started = monotonic()
    response_code: int | None = None
    result = DeliveryAttemptResult.DELIVERED
    error_text: str | None = None

    try:
        delivery = deliver_normalized_event(
            normalized_event,
            downstream_url=downstream_url,
            timeout_seconds=timeout_seconds,
            client=client,
        )
        response_code = delivery.downstream_status_code
    except httpx.HTTPStatusError as error:
        result = DeliveryAttemptResult.HTTP_ERROR
        response_code = error.response.status_code
        error_text = str(error)[:1000]
        raise
    except httpx.TransportError as error:
        result = DeliveryAttemptResult.TRANSPORT_ERROR
        error_text = str(error)[:1000]
        raise
    finally:
        completed_at = utcnow()
        latency_ms = max(0, round((monotonic() - started) * 1000))
        record_delivery_attempt(
            event_id=event_id,
            result=result,
            response_code=response_code,
            latency_ms=latency_ms,
            error=error_text,
            started_at=started_at,
            completed_at=completed_at,
            sessions=sessions,
        )

    return delivery
