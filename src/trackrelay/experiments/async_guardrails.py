"""Define machine-checkable asynchronous processing and drain guardrails."""

from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from trackrelay.domain import DeliveryAttemptResult
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.models import DeliveryAttempt, DeliveryOutboxEntry, Event

Count = Annotated[int, Field(ge=0)]
Seconds = Annotated[float, Field(ge=0)]
PositiveSeconds = Annotated[float, Field(gt=0)]


class AsyncProcessingGuardrailDefinition(BaseModel):
    """Frozen completion and post-load drain requirements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: Literal["async-processing-v1"] = "async-processing-v1"
    drain_deadline_seconds: PositiveSeconds = 120
    empty_stability_seconds: PositiveSeconds = 180
    maximum_observation_gap_seconds: PositiveSeconds = 20

    @model_validator(mode="after")
    def require_meaningful_sampling_interval(
        self,
    ) -> "AsyncProcessingGuardrailDefinition":
        if (
            self.maximum_observation_gap_seconds
            > self.empty_stability_seconds
        ):
            raise ValueError(
                "maximum observation gap cannot exceed the stability window"
            )
        return self


ASYNC_PROCESSING_GUARDRAILS = AsyncProcessingGuardrailDefinition()


class AsyncProcessingObservation(BaseModel):
    """One aligned database and queue observation after offered load ends."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: AwareDatetime
    seconds_after_load_ended: Seconds
    accepted_unique_events: Count
    durable_outbox_entries: Count
    completed_delivery_events: Count
    pending_outbox_entries: Count
    source_queue_visible_messages: Count
    source_queue_in_flight_messages: Count
    source_queue_delayed_messages: Count
    dead_letter_queue_messages: Count

    @model_validator(mode="after")
    def require_possible_database_counts(self) -> "AsyncProcessingObservation":
        if self.durable_outbox_entries > self.accepted_unique_events:
            raise ValueError(
                "durable outbox entries cannot exceed accepted unique events"
            )
        if self.completed_delivery_events > self.accepted_unique_events:
            raise ValueError(
                "completed deliveries cannot exceed accepted unique events"
            )
        if self.pending_outbox_entries > self.durable_outbox_entries:
            raise ValueError(
                "pending outbox entries cannot exceed durable outbox entries"
            )
        return self

    @computed_field
    @property
    def incomplete_delivery_events(self) -> int:
        return self.accepted_unique_events - self.completed_delivery_events

    @computed_field
    @property
    def processing_drained(self) -> bool:
        """Whether no accepted or queued work remains at this observation."""
        return (
            self.durable_outbox_entries == self.accepted_unique_events
            and self.incomplete_delivery_events == 0
            and self.pending_outbox_entries == 0
            and self.source_queue_visible_messages == 0
            and self.source_queue_in_flight_messages == 0
            and self.source_queue_delayed_messages == 0
            and self.dead_letter_queue_messages == 0
        )


class AsyncProcessingEvidence(BaseModel):
    """Reconciliation plus an ordered post-load processing timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    test_run_id: UUID
    reconciliation: ReconciliationReport
    observations: tuple[AsyncProcessingObservation, ...]

    @model_validator(mode="after")
    def require_one_consistent_timeline(self) -> "AsyncProcessingEvidence":
        if self.test_run_id != self.reconciliation.test_run_id:
            raise ValueError("evidence and reconciliation test-run IDs differ")
        if not self.observations:
            raise ValueError("at least one processing observation is required")

        elapsed_times = tuple(
            observation.seconds_after_load_ended
            for observation in self.observations
        )
        if any(
            later <= earlier
            for earlier, later in pairwise(elapsed_times)
        ):
            raise ValueError("processing observation times must increase")

        for earlier, later in pairwise(self.observations):
            observed_delta = (
                later.observed_at - earlier.observed_at
            ).total_seconds()
            elapsed_delta = (
                later.seconds_after_load_ended
                - earlier.seconds_after_load_ended
            )
            if abs(observed_delta - elapsed_delta) > 1e-6:
                raise ValueError(
                    "processing observation timestamps and elapsed times differ"
                )

        accepted_counts = {
            observation.accepted_unique_events
            for observation in self.observations
        }
        outbox_counts = {
            observation.durable_outbox_entries
            for observation in self.observations
        }
        if len(accepted_counts) != 1 or len(outbox_counts) != 1:
            raise ValueError(
                "accepted-event and durable-outbox counts must remain stable "
                "after offered load ends"
            )
        completed_counts = tuple(
            observation.completed_delivery_events
            for observation in self.observations
        )
        if any(
            later < earlier
            for earlier, later in pairwise(completed_counts)
        ):
            raise ValueError("completed delivery count cannot decrease")
        return self


class AsyncProcessingGuardrailEvaluation(BaseModel):
    """Pass/fail interpretation of one asynchronous processing timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: AsyncProcessingGuardrailDefinition
    evidence: AsyncProcessingEvidence

    def _drain_window(self) -> tuple[float, float] | None:
        stable_since: float | None = None
        previous_seconds: float | None = None
        for observation in self.evidence.observations:
            if (
                previous_seconds is not None
                and observation.seconds_after_load_ended - previous_seconds
                > self.definition.maximum_observation_gap_seconds
            ):
                stable_since = None
            previous_seconds = observation.seconds_after_load_ended
            if not observation.processing_drained:
                stable_since = None
                continue
            if stable_since is None:
                stable_since = observation.seconds_after_load_ended
            if (
                observation.seconds_after_load_ended - stable_since
                >= self.definition.empty_stability_seconds
            ):
                return stable_since, observation.seconds_after_load_ended
        return None

    @computed_field
    @property
    def drain_reached_after_seconds(self) -> float | None:
        drain_window = self._drain_window()
        return drain_window[0] if drain_window is not None else None

    @computed_field
    @property
    def drain_confirmed_after_seconds(self) -> float | None:
        drain_window = self._drain_window()
        return drain_window[1] if drain_window is not None else None

    @computed_field
    @property
    def accepted_events_accounted_for(self) -> bool:
        final = self.evidence.observations[-1]
        reconciliation = self.evidence.reconciliation
        return (
            final.accepted_unique_events == reconciliation.unique
            and final.durable_outbox_entries == reconciliation.unique
            and final.completed_delivery_events == reconciliation.unique
            and reconciliation.simulator_unique_events == reconciliation.unique
            and reconciliation.unaccounted == 0
        )

    @computed_field
    @property
    def duplicate_effect_guardrail_passed(self) -> bool:
        return self.evidence.reconciliation.duplicate_business_effects == 0

    @computed_field
    @property
    def final_shipment_guardrail_passed(self) -> bool:
        return (
            self.evidence.reconciliation.incorrect_final_shipment_states == 0
        )

    @computed_field
    @property
    def dead_letter_guardrail_passed(self) -> bool:
        return all(
            observation.dead_letter_queue_messages == 0
            for observation in self.evidence.observations
        )

    @computed_field
    @property
    def drain_deadline_guardrail_passed(self) -> bool:
        drain_window = self._drain_window()
        return (
            drain_window is not None
            and drain_window[0] <= self.definition.drain_deadline_seconds
        )

    @computed_field
    @property
    def guardrails_passed(self) -> bool:
        return (
            self.evidence.reconciliation.invariants_passed
            and self.accepted_events_accounted_for
            and self.duplicate_effect_guardrail_passed
            and self.final_shipment_guardrail_passed
            and self.dead_letter_guardrail_passed
            and self.drain_deadline_guardrail_passed
        )


def capture_database_processing_observation(
    *,
    test_run_id: UUID,
    load_ended_at: datetime,
    observed_at: datetime | None = None,
    session: Session,
    source_queue_visible_messages: int,
    source_queue_in_flight_messages: int,
    source_queue_delayed_messages: int,
    dead_letter_queue_messages: int,
) -> AsyncProcessingObservation:
    """Align database completion state with one externally observed queue state."""
    observed_at = observed_at or datetime.now(UTC)
    if load_ended_at.tzinfo is None or observed_at.tzinfo is None:
        raise ValueError("processing observation timestamps must be timezone-aware")
    seconds_after_load_ended = (
        observed_at.astimezone(UTC) - load_ended_at.astimezone(UTC)
    ).total_seconds()
    if seconds_after_load_ended < 0:
        raise ValueError("processing observation cannot precede load completion")

    accepted_unique_events = session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.test_run_id == test_run_id)
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
    completed_delivery_events = session.scalar(
        select(func.count(distinct(DeliveryAttempt.event_id)))
        .select_from(DeliveryAttempt)
        .join(Event, DeliveryAttempt.event_id == Event.id)
        .where(
            Event.test_run_id == test_run_id,
            DeliveryAttempt.result == DeliveryAttemptResult.DELIVERED,
        )
    )

    return AsyncProcessingObservation(
        observed_at=observed_at,
        seconds_after_load_ended=seconds_after_load_ended,
        accepted_unique_events=accepted_unique_events or 0,
        durable_outbox_entries=durable_outbox_entries or 0,
        completed_delivery_events=completed_delivery_events or 0,
        pending_outbox_entries=pending_outbox_entries or 0,
        source_queue_visible_messages=source_queue_visible_messages,
        source_queue_in_flight_messages=source_queue_in_flight_messages,
        source_queue_delayed_messages=source_queue_delayed_messages,
        dead_letter_queue_messages=dead_letter_queue_messages,
    )


def evaluate_async_processing_guardrails(
    evidence: AsyncProcessingEvidence,
    *,
    definition: AsyncProcessingGuardrailDefinition = (
        ASYNC_PROCESSING_GUARDRAILS
    ),
) -> AsyncProcessingGuardrailEvaluation:
    """Evaluate completion, correctness, DLQ, and drain-deadline evidence."""
    return AsyncProcessingGuardrailEvaluation(
        definition=definition,
        evidence=evidence,
    )
