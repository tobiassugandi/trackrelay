"""TrackRelay API application."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import distinct, func, select
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
from trackrelay.experiments.generator import InputManifest
from trackrelay.experiments.reconciliation import (
    ReconciliationReport,
    TestRunDefinitionMismatchError,
    TestRunNotFoundError,
    fetch_simulator_receipts,
    reconcile_manifest,
)
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Partner,
    Shipment,
)
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.partners import PARTNER_ADAPTERS
from trackrelay.request_timing import RequestTimingMiddleware, ingestion_stage
from trackrelay.runtime_metrics import RuntimeMetricsSnapshot, capture_runtime_metrics
from trackrelay.scaling_metrics import scaling_metrics_lifespan
from trackrelay.services import (
    DownstreamDeliveryQueue,
    DownstreamDeliveryQueueError,
    EventPersistenceResult,
    RecordingDownstreamDeliveryQueue,
    list_shipment_events,
    persist_normalized_event,
    publish_delivery_outbox_entry,
)
from trackrelay.services.experiment_reset import (
    ExperimentResetError,
    ExperimentResetEvidence,
    ExperimentStateSnapshot,
    inspect_experiment_state,
    reset_experiment_state,
)
from trackrelay.sqs_delivery import SqsDownstreamDeliveryQueue, create_sqs_client

settings = Settings()
app = FastAPI(
    title=settings.app_name, debug=settings.debug, lifespan=scaling_metrics_lifespan
)
logger = logging.getLogger(__name__)
app.add_middleware(
    RequestTimingMiddleware, enabled=bool(settings.scaling_metrics_queue_name)
)

EventPersister = Callable[[NormalizedEvent], EventPersistenceResult]
DeliveryOutboxPublisher = Callable[
    [UUID, DownstreamDeliveryQueue],
    bool,
]
NonNegativeCount = Annotated[int, Field(ge=0)]
local_downstream_delivery_queue = RecordingDownstreamDeliveryQueue()


class QueuedEventResponse(BaseModel):
    """Outcome when a new logical event is persisted and scheduled."""

    event_id: UUID
    processing_status: Literal["processed"]
    duplicate: Literal[False]
    delivery_status: Literal["queued"]
    downstream_status_code: None


class DuplicateEventResponse(BaseModel):
    """Contract for a retry that resolves to an existing logical event."""

    event_id: UUID
    processing_status: Literal["processed"]
    duplicate: Literal[True]
    delivery_status: Literal["skipped_duplicate"]
    downstream_status_code: None


IngestionResponse = QueuedEventResponse | DuplicateEventResponse


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
    test_run_id: UUID | None
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


class DatabaseEventSummary(BaseModel):
    """Persisted event counts for one test run."""

    persisted: NonNegativeCount
    processed: NonNegativeCount
    failed: NonNegativeCount
    pending: NonNegativeCount


class DatabaseDeliveryAttemptSummary(BaseModel):
    """Persisted downstream-attempt counts for one test run."""

    total: NonNegativeCount
    delivered: NonNegativeCount
    delivered_unique_events: NonNegativeCount
    http_error: NonNegativeCount
    transport_error: NonNegativeCount


class DatabaseOutboxSummary(BaseModel):
    """Durable publication state for one test run."""

    durable: NonNegativeCount
    pending_publication: NonNegativeCount


class TestRunSummaryResponse(BaseModel):
    """Database-backed, machine-readable summary of one test run."""

    schema_version: Literal[1] = 1
    test_run_id: UUID
    scenario_name: str
    random_seed: int
    configuration: dict[str, JsonValue]
    declared_event_count: NonNegativeCount
    started_at: datetime
    completed_at: datetime | None
    database_events: DatabaseEventSummary
    database_delivery_attempts: DatabaseDeliveryAttemptSummary
    database_outbox: DatabaseOutboxSummary


class TestRunLifecycleResponse(BaseModel):
    """Identity and lifecycle state for one registered synthetic run."""

    schema_version: Literal[1] = 1
    test_run_id: UUID
    status: Literal["registered", "completed"]


class ExperimentResetRequest(BaseModel):
    """Exact qualified run the controller authorizes for destructive reset."""

    model_config = ConfigDict(extra="forbid")

    test_run_id: UUID
    expected_event_count: Annotated[int, Field(gt=0)]


def get_event_persister() -> EventPersister:
    """Provide the application service used to persist normalized events."""
    return persist_normalized_event


def get_delivery_outbox_publisher() -> DeliveryOutboxPublisher:
    """Provide the post-commit fast-path outbox publisher."""
    return publish_delivery_outbox_entry


def get_downstream_delivery_queue() -> DownstreamDeliveryQueue:
    """Provide the configured queue without exposing its transport to ingestion."""
    if settings.delivery_queue_backend == "sqs":
        return get_sqs_downstream_delivery_queue()
    return local_downstream_delivery_queue


@lru_cache(maxsize=1)
def get_sqs_downstream_delivery_queue() -> SqsDownstreamDeliveryQueue:
    """Create one process-wide SQS publisher and reuse its connection pool."""
    if not settings.sqs_queue_url:
        raise RuntimeError("SQS queue URL is required for the SQS queue backend")
    return SqsDownstreamDeliveryQueue(
        client=create_sqs_client(region_name=settings.aws_region),
        queue_url=settings.sqs_queue_url,
    )


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
    "/api/v1/experiments/runtime-metrics",
    response_model=RuntimeMetricsSnapshot,
    tags=["experiments"],
)
def get_runtime_metrics() -> RuntimeMetricsSnapshot:
    """Expose one read-only API-process sample for local experiments."""
    return capture_runtime_metrics()


@app.get(
    "/api/v1/experiments/state",
    response_model=ExperimentStateSnapshot,
    tags=["experiments"],
)
def get_experiment_state(
    session: Annotated[Session, Depends(get_session)],
) -> ExperimentStateSnapshot:
    """Expose only compact counts needed to prove treatment isolation."""
    try:
        with httpx.Client(
            base_url=settings.downstream_url,
            timeout=settings.downstream_timeout_seconds,
        ) as downstream_client:
            return inspect_experiment_state(
                session=session,
                downstream_client=downstream_client,
            )
    except (httpx.HTTPError, ExperimentResetError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment state evidence is unavailable",
        ) from error


@app.post(
    "/api/v1/experiments/reset",
    response_model=ExperimentResetEvidence,
    tags=["experiments"],
)
def reset_experiment(
    request: ExperimentResetRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ExperimentResetEvidence:
    """Clear only the sole exact synthetic run between approved treatments."""
    try:
        with httpx.Client(
            base_url=settings.downstream_url,
            timeout=settings.downstream_timeout_seconds,
        ) as downstream_client:
            return reset_experiment_state(
                session=session,
                downstream_client=downstream_client,
                test_run_id=request.test_run_id,
                expected_event_count=request.expected_event_count,
            )
    except ExperimentResetError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulator reset is unavailable",
        ) from error


@app.get(
    "/api/v1/test-runs/{test_run_id}/summary",
    response_model=TestRunSummaryResponse,
    tags=["test runs"],
)
def get_test_run_summary(
    test_run_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> TestRunSummaryResponse:
    """Summarize one run using evidence persisted in TrackRelay's database."""
    database_test_run = session.get(ExperimentRunModel, test_run_id)
    if database_test_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )

    database_event_count_by_status = {
        processing_status: event_count
        for processing_status, event_count in session.execute(
            select(Event.processing_status, func.count(Event.id))
            .where(Event.test_run_id == test_run_id)
            .group_by(Event.processing_status)
        ).tuples()
    }
    database_delivery_attempt_count_by_result = {
        attempt_result: attempt_count
        for attempt_result, attempt_count in session.execute(
            select(DeliveryAttempt.result, func.count(DeliveryAttempt.id))
            .join(Event, DeliveryAttempt.event_id == Event.id)
            .where(Event.test_run_id == test_run_id)
            .group_by(DeliveryAttempt.result)
        ).tuples()
    }
    delivered_unique_events = session.scalar(
        select(func.count(distinct(DeliveryAttempt.event_id)))
        .join(Event, DeliveryAttempt.event_id == Event.id)
        .where(
            Event.test_run_id == test_run_id,
            DeliveryAttempt.result == DeliveryAttemptResult.DELIVERED,
        )
    )
    durable_outbox_entries = session.scalar(
        select(func.count())
        .select_from(DeliveryOutboxEntry)
        .join(Event, DeliveryOutboxEntry.event_id == Event.id)
        .where(Event.test_run_id == test_run_id)
    )
    pending_outbox_entries = session.scalar(
        select(func.count())
        .select_from(DeliveryOutboxEntry)
        .join(Event, DeliveryOutboxEntry.event_id == Event.id)
        .where(
            Event.test_run_id == test_run_id,
            DeliveryOutboxEntry.published_at.is_(None),
        )
    )

    return TestRunSummaryResponse(
        test_run_id=database_test_run.id,
        scenario_name=database_test_run.scenario_name,
        random_seed=database_test_run.random_seed,
        configuration=database_test_run.configuration,
        declared_event_count=database_test_run.expected_event_count,
        started_at=database_test_run.started_at,
        completed_at=database_test_run.completed_at,
        database_events=DatabaseEventSummary(
            persisted=sum(database_event_count_by_status.values()),
            processed=database_event_count_by_status.get(
                EventProcessingStatus.PROCESSED,
                0,
            ),
            failed=database_event_count_by_status.get(
                EventProcessingStatus.FAILED,
                0,
            ),
            pending=database_event_count_by_status.get(
                EventProcessingStatus.RECEIVED,
                0,
            ),
        ),
        database_delivery_attempts=DatabaseDeliveryAttemptSummary(
            total=sum(database_delivery_attempt_count_by_result.values()),
            delivered=database_delivery_attempt_count_by_result.get(
                DeliveryAttemptResult.DELIVERED,
                0,
            ),
            delivered_unique_events=delivered_unique_events or 0,
            http_error=database_delivery_attempt_count_by_result.get(
                DeliveryAttemptResult.HTTP_ERROR,
                0,
            ),
            transport_error=database_delivery_attempt_count_by_result.get(
                DeliveryAttemptResult.TRANSPORT_ERROR,
                0,
            ),
        ),
        database_outbox=DatabaseOutboxSummary(
            durable=durable_outbox_entries or 0,
            pending_publication=pending_outbox_entries or 0,
        ),
    )


@app.post(
    "/api/v1/test-runs",
    status_code=status.HTTP_201_CREATED,
    response_model=TestRunLifecycleResponse,
    tags=["test runs"],
)
def register_test_run(
    manifest: InputManifest,
    session: Annotated[Session, Depends(get_session)],
) -> TestRunLifecycleResponse:
    """Register one synthetic manifest before accepting its event requests."""
    if session.get(ExperimentRunModel, manifest.test_run_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Test run already exists",
        )
    partner = session.get(Partner, manifest.configuration.partner_id)
    if partner is None:
        session.add(
            Partner(
                id=manifest.configuration.partner_id,
                name=f"Experiment {manifest.configuration.partner_id}",
                adapter_type="courier-alpha",
                is_active=True,
            )
        )
    elif partner.adapter_type != "courier-alpha" or not partner.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Test-run partner must be active and use courier-alpha",
        )
    session.add(
        ExperimentRunModel(
            id=manifest.test_run_id,
            scenario_name=manifest.scenario_name,
            random_seed=manifest.seed,
            configuration=manifest.configuration.model_dump(mode="json"),
            expected_event_count=manifest.events_generated,
        )
    )
    session.commit()
    return TestRunLifecycleResponse(
        test_run_id=manifest.test_run_id,
        status="registered",
    )


@app.post(
    "/api/v1/test-runs/{test_run_id}/complete",
    response_model=TestRunLifecycleResponse,
    tags=["test runs"],
)
def complete_test_run(
    test_run_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> TestRunLifecycleResponse:
    """Freeze the server-observed end of one synthetic offered-load window."""
    database_test_run = session.get(ExperimentRunModel, test_run_id)
    if database_test_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Test run not found",
        )
    if database_test_run.completed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Test run is already complete",
        )
    database_test_run.completed_at = datetime.now(UTC)
    session.commit()
    return TestRunLifecycleResponse(
        test_run_id=test_run_id,
        status="completed",
    )


@app.post(
    "/api/v1/test-runs/{test_run_id}/reconciliation",
    response_model=ReconciliationReport,
    tags=["test runs"],
)
def reconcile_test_run(
    test_run_id: UUID,
    manifest: InputManifest,
    session: Annotated[Session, Depends(get_session)],
) -> ReconciliationReport:
    """Reconcile one manifest using private database and simulator evidence."""
    if manifest.test_run_id != test_run_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manifest and path test-run IDs differ",
        )
    try:
        simulator_receipts = fetch_simulator_receipts(
            settings.downstream_url,
            test_run_id=test_run_id,
            timeout_seconds=settings.downstream_timeout_seconds,
        )
        return reconcile_manifest(
            manifest,
            session=session,
            simulator_receipts=simulator_receipts,
        )
    except (TestRunNotFoundError, TestRunDefinitionMismatchError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulator reconciliation evidence is unavailable",
        ) from error


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
        test_run_id=event.test_run_id,
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
    tags=["events"],
)
def ingest_partner_event(
    partner_id: str,
    payload: dict[str, JsonValue],
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    persist_event: Annotated[EventPersister, Depends(get_event_persister)],
    publish_outbox_entry: Annotated[
        DeliveryOutboxPublisher,
        Depends(get_delivery_outbox_publisher),
    ],
    delivery_queue: Annotated[
        DownstreamDeliveryQueue,
        Depends(get_downstream_delivery_queue),
    ],
    test_run_id: Annotated[UUID | None, Header(alias="X-Test-Run-ID")] = None,
) -> IngestionResponse:
    """Validate, normalize, persist, and schedule one configured partner event."""
    with ingestion_stage("partner_lookup"):
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
    adapter = PARTNER_ADAPTERS.get(partner.adapter_type)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Partner adapter is not supported",
        )
    configured_partner_id = partner.id
    # Release the lookup connection before durable acceptance acquires its own.
    session.close()

    try:
        validated_payload = adapter.payload_model.model_validate(payload)
    except ValidationError as error:
        errors = [{**item, "loc": ("body", *item["loc"])} for item in error.errors()]
        raise RequestValidationError(errors) from error

    normalized_event = adapter.normalize(
        validated_payload,
        partner_id=configured_partner_id,
        received_at=datetime.now(UTC),
    )
    if test_run_id is not None:
        normalized_event = normalized_event.model_copy(
            update={"test_run_id": test_run_id}
        )
    with ingestion_stage("persistence"):
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
        with ingestion_stage("publication"):
            publish_outbox_entry(persistence.event_id, delivery_queue)
    except (DownstreamDeliveryQueueError, SQLAlchemyError):
        logger.warning(
            "immediate delivery publication failed; durable outbox remains pending",
            extra={"event_id": str(persistence.event_id)},
            exc_info=True,
        )

    return QueuedEventResponse(
        event_id=persistence.event_id,
        processing_status="processed",
        duplicate=False,
        delivery_status="queued",
        downstream_status_code=None,
    )
