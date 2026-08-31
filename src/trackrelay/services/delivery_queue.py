"""Application boundary and deterministic fake for downstream delivery work."""

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


class DownstreamDeliveryQueueError(RuntimeError):
    """Report that a downstream job could not be scheduled."""


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
