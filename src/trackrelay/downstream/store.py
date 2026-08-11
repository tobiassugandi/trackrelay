"""In-memory event storage for the downstream simulator."""

from threading import Lock

from trackrelay.domain import NormalizedEvent


class DownstreamEventStore:
    """Record normalized events safely within one simulator process."""

    def __init__(self) -> None:
        self._events: list[NormalizedEvent] = []
        self._lock = Lock()

    def record(self, event: NormalizedEvent) -> int:
        """Store an event and return the total received count."""
        with self._lock:
            self._events.append(event)
            return len(self._events)

    def all(self) -> tuple[NormalizedEvent, ...]:
        """Return an immutable snapshot of received events."""
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        """Remove all events, primarily to isolate tests."""
        with self._lock:
            self._events.clear()
