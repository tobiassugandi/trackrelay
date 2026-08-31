"""In-memory event storage for the downstream simulator."""

from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from trackrelay.domain import NormalizedEvent


@dataclass(frozen=True)
class DownstreamRecordResult:
    """Result of atomically recording one downstream request."""

    received_count: int
    duplicate: bool


class DownstreamEventStore:
    """Record normalized events safely within one simulator process."""

    def __init__(self) -> None:
        self._events: list[NormalizedEvent] = []
        self._idempotency_keys: set[UUID] = set()
        self._lock = Lock()

    def record(
        self,
        event: NormalizedEvent,
        *,
        idempotency_key: UUID | None = None,
    ) -> DownstreamRecordResult:
        """Atomically store a new request or identify an idempotent replay."""
        with self._lock:
            if (
                idempotency_key is not None
                and idempotency_key in self._idempotency_keys
            ):
                return DownstreamRecordResult(
                    received_count=len(self._events),
                    duplicate=True,
                )

            self._events.append(event)
            if idempotency_key is not None:
                self._idempotency_keys.add(idempotency_key)
            return DownstreamRecordResult(
                received_count=len(self._events),
                duplicate=False,
            )

    def all(self) -> tuple[NormalizedEvent, ...]:
        """Return an immutable snapshot of received events."""
        with self._lock:
            return tuple(self._events)

    def clear(self) -> int:
        """Remove all events and report how many receipts were discarded."""
        with self._lock:
            cleared_count = len(self._events)
            self._events.clear()
            self._idempotency_keys.clear()
            return cleared_count
