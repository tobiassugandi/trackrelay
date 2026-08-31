"""Application boundary and deterministic fake for downstream delivery work."""

from collections.abc import Callable
from threading import Lock
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DownstreamDeliveryJob(BaseModel):
    """Identify one persisted event that still needs downstream delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: UUID


@runtime_checkable
class DownstreamDeliveryQueue(Protocol):
    """Accept downstream jobs without exposing a particular queue service."""

    def enqueue(self, job: DownstreamDeliveryJob) -> None:
        """Schedule one persisted event for downstream delivery."""
        ...


@runtime_checkable
class DownstreamDeliveryMessage(Protocol):
    """Expose one received job and its transport acknowledgement."""

    @property
    def job(self) -> DownstreamDeliveryJob:
        """Return the validated job carried by this message."""
        ...

    @property
    def receive_count(self) -> int:
        """Return how many times the queue has delivered this message."""
        ...

    def acknowledge(self) -> None:
        """Remove a successfully processed message from future delivery."""
        ...


@runtime_checkable
class DownstreamDeliveryReceiver(Protocol):
    """Receive a bounded batch of downstream-delivery messages."""

    def receive(self) -> tuple[DownstreamDeliveryMessage, ...]:
        """Return immediately available messages after a bounded wait."""
        ...


class DownstreamDeliveryQueueError(RuntimeError):
    """Report that a downstream job could not be scheduled."""


class DownstreamDeliveryReceiveError(RuntimeError):
    """Report that messages could not be received from the queue."""


class DownstreamDeliveryMessageDecodeError(ValueError):
    """Report a received message outside the versioned job contract."""


class DownstreamDeliveryAcknowledgementError(RuntimeError):
    """Report that a processed message could not be acknowledged."""


class RecordingDownstreamDeliveryQueue:
    """Record jobs deterministically for local development and focused tests.

    This process-local fake is deliberately not a durable queue. It keeps the
    API runnable while ingestion and worker behavior are developed without a
    local AWS emulator or substitute message broker.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._enqueued_jobs: list[DownstreamDeliveryJob] = []

    @property
    def enqueued_jobs(self) -> tuple[DownstreamDeliveryJob, ...]:
        """Return an immutable snapshot in enqueue order."""
        with self._lock:
            return tuple(self._enqueued_jobs)

    def enqueue(self, job: DownstreamDeliveryJob) -> None:
        """Record one enqueue call without deduplicating it."""
        with self._lock:
            self._enqueued_jobs.append(job)

    def clear(self) -> None:
        """Reset process-local state between deterministic scenarios."""
        with self._lock:
            self._enqueued_jobs.clear()


class RecordingDownstreamDeliveryMessage:
    """Record acknowledgement calls for deterministic worker tests."""

    def __init__(
        self,
        job: DownstreamDeliveryJob,
        *,
        acknowledgement_failure: Exception | None = None,
        on_acknowledge: Callable[[], None] | None = None,
        receive_count: int = 1,
    ) -> None:
        if receive_count < 1:
            raise ValueError("message receive count must be positive")
        self._job = job
        self._acknowledgement_failure = acknowledgement_failure
        self._on_acknowledge = on_acknowledge
        self._acknowledgement_calls = 0
        self._acknowledged = False
        self._receive_count = receive_count

    @property
    def job(self) -> DownstreamDeliveryJob:
        """Return the received job unchanged."""
        return self._job

    @property
    def acknowledgement_calls(self) -> int:
        """Return how many times acknowledgement was attempted."""
        return self._acknowledgement_calls

    @property
    def receive_count(self) -> int:
        """Return the deterministic delivery count configured by the test."""
        return self._receive_count

    @property
    def acknowledged(self) -> bool:
        """Report whether acknowledgement completed successfully."""
        return self._acknowledged

    def acknowledge(self) -> None:
        """Record the call and optionally reproduce a transport failure."""
        self._acknowledgement_calls += 1
        if self._on_acknowledge is not None:
            self._on_acknowledge()
        if self._acknowledgement_failure is not None:
            raise self._acknowledgement_failure
        self._acknowledged = True
