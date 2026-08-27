"""Application boundary for scheduling downstream delivery work."""

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
