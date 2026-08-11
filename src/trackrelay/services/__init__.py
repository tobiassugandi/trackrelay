"""Application services coordinating domain and persistence work."""

from trackrelay.services.event_persistence import persist_normalized_event

__all__ = ["persist_normalized_event"]
