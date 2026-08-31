"""Tests for asynchronous completion and queue-drain guardrails."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.experiments.async_guardrails import (
    ASYNC_PROCESSING_GUARDRAILS,
    AsyncProcessingEvidence,
    AsyncProcessingGuardrailDefinition,
    AsyncProcessingObservation,
    capture_database_processing_observation,
    evaluate_async_processing_guardrails,
)
from trackrelay.experiments.reconciliation import ReconciliationReport
from trackrelay.models import DeliveryAttempt, DeliveryOutboxEntry, Partner
from trackrelay.models import TestRun as ExperimentRunModel
from trackrelay.services import persist_normalized_event

TEST_RUN_ID = UUID("00000000-0000-0000-0000-000000000991")
LOAD_ENDED_AT = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def reconciliation_report(
    *,
    unique: int = 5,
    simulator_unique_events: int = 5,
    duplicate_business_effects: int = 0,
    incorrect_final_shipment_states: int = 0,
    unaccounted: int = 0,
) -> ReconciliationReport:
    invariants_passed = (
        duplicate_business_effects == 0
        and incorrect_final_shipment_states == 0
        and unaccounted == 0
    )
    return ReconciliationReport(
        test_run_id=TEST_RUN_ID,
        generated=unique,
        accepted=unique,
        rejected=0,
        unique=unique,
        processed=unique,
        failed=0,
        pending=0,
        unaccounted=unaccounted,
        simulator_receipts=simulator_unique_events,
        simulator_unique_events=simulator_unique_events,
        duplicate_business_effects=duplicate_business_effects,
        incorrect_final_shipment_states=incorrect_final_shipment_states,
        invariants_passed=invariants_passed,
    )


def observation(
    seconds_after_load_ended: float,
    *,
    completed: int,
    pending_outbox: int = 0,
    visible: int = 0,
    in_flight: int = 0,
    delayed: int = 0,
    dead_letter: int = 0,
) -> AsyncProcessingObservation:
    return AsyncProcessingObservation(
        observed_at=LOAD_ENDED_AT
        + timedelta(seconds=seconds_after_load_ended),
        seconds_after_load_ended=seconds_after_load_ended,
        accepted_unique_events=5,
        durable_outbox_entries=5,
        completed_delivery_events=completed,
        pending_outbox_entries=pending_outbox,
        source_queue_visible_messages=visible,
        source_queue_in_flight_messages=in_flight,
        source_queue_delayed_messages=delayed,
        dead_letter_queue_messages=dead_letter,
    )


def evidence(
    *observations: AsyncProcessingObservation,
    reconciliation: ReconciliationReport | None = None,
) -> AsyncProcessingEvidence:
    return AsyncProcessingEvidence(
        test_run_id=TEST_RUN_ID,
        reconciliation=reconciliation or reconciliation_report(),
        observations=observations,
    )


def test_guardrail_definition_freezes_the_drain_contract() -> None:
    assert ASYNC_PROCESSING_GUARDRAILS.model_dump() == {
        "schema_version": 1,
        "name": "async-processing-v1",
        "drain_deadline_seconds": 120.0,
        "empty_stability_seconds": 180.0,
        "maximum_observation_gap_seconds": 20.0,
    }

    with raises(ValidationError, match="cannot exceed the stability window"):
        AsyncProcessingGuardrailDefinition(
            empty_stability_seconds=10,
            maximum_observation_gap_seconds=20,
        )


def test_guardrails_require_completion_and_a_stably_empty_backlog() -> None:
    evaluation = evaluate_async_processing_guardrails(
        evidence(
            observation(0, completed=2, visible=2, in_flight=1),
            *(
                observation(seconds, completed=5)
                for seconds in range(60, 241, 20)
            ),
        )
    )

    assert evaluation.drain_reached_after_seconds == 60
    assert evaluation.drain_confirmed_after_seconds == 240
    assert evaluation.accepted_events_accounted_for is True
    assert evaluation.duplicate_effect_guardrail_passed is True
    assert evaluation.final_shipment_guardrail_passed is True
    assert evaluation.dead_letter_guardrail_passed is True
    assert evaluation.drain_deadline_guardrail_passed is True
    assert evaluation.guardrails_passed is True


def test_empty_queue_does_not_hide_an_incomplete_delivery() -> None:
    evaluation = evaluate_async_processing_guardrails(
        evidence(
            observation(0, completed=4),
            observation(180, completed=4),
        )
    )

    assert evaluation.drain_confirmed_after_seconds is None
    assert evaluation.accepted_events_accounted_for is False
    assert evaluation.drain_deadline_guardrail_passed is False
    assert evaluation.guardrails_passed is False


def test_dlq_or_late_drain_fails_even_if_final_reconciliation_is_clean() -> None:
    evaluation = evaluate_async_processing_guardrails(
        evidence(
            observation(0, completed=4, visible=1, dead_letter=1),
            *(
                observation(seconds, completed=5)
                for seconds in range(125, 306, 20)
            ),
        )
    )

    assert evaluation.drain_reached_after_seconds == 125
    assert evaluation.drain_confirmed_after_seconds == 305
    assert evaluation.dead_letter_guardrail_passed is False
    assert evaluation.drain_deadline_guardrail_passed is False
    assert evaluation.guardrails_passed is False


def test_processing_evidence_rejects_an_incoherent_timeline() -> None:
    with raises(ValidationError, match="times must increase"):
        evidence(
            observation(10, completed=4),
            observation(5, completed=5),
        )

    with raises(ValidationError, match="cannot decrease"):
        evidence(
            observation(0, completed=4),
            observation(10, completed=3),
        )

    contradictory = observation(10, completed=5).model_copy(
        update={"observed_at": LOAD_ENDED_AT + timedelta(seconds=20)}
    )
    with raises(ValidationError, match="timestamps and elapsed times differ"):
        evidence(
            observation(0, completed=4),
            contradictory,
        )


def test_correctness_failures_cannot_be_hidden_by_an_empty_queue() -> None:
    evaluation = evaluate_async_processing_guardrails(
        evidence(
            *(
                observation(seconds, completed=5)
                for seconds in range(0, 181, 20)
            ),
            reconciliation=reconciliation_report(
                duplicate_business_effects=1,
                incorrect_final_shipment_states=1,
            ),
        )
    )

    assert evaluation.drain_deadline_guardrail_passed is True
    assert evaluation.duplicate_effect_guardrail_passed is False
    assert evaluation.final_shipment_guardrail_passed is False
    assert evaluation.guardrails_passed is False


def test_sparse_zero_observations_do_not_claim_continuous_drain() -> None:
    evaluation = evaluate_async_processing_guardrails(
        evidence(
            observation(0, completed=5),
            observation(180, completed=5),
        )
    )

    assert evaluation.drain_reached_after_seconds is None
    assert evaluation.drain_confirmed_after_seconds is None
    assert evaluation.drain_deadline_guardrail_passed is False


def test_database_observation_counts_durable_and_completed_work() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            ExperimentRunModel(
                id=TEST_RUN_ID,
                scenario_name="async-guardrail-test",
                random_seed=20260831,
                configuration={"events": 2},
                expected_event_count=2,
            )
        )
        session.add(
            Partner(
                id="courier-alpha",
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )

    event_ids = []
    for index in (1, 2):
        persisted = persist_normalized_event(
            NormalizedEvent(
                partner_id="courier-alpha",
                partner_event_id=f"GUARDRAIL-{index}",
                tracking_number=f"GUARDRAIL-TRK-{index}",
                status=ShipmentStatus.PICKED_UP,
                occurred_at=LOAD_ENDED_AT - timedelta(minutes=1),
                received_at=LOAD_ENDED_AT - timedelta(seconds=59),
                raw_payload={"status": "PICKUP"},
                test_run_id=TEST_RUN_ID,
            ),
            sessions=sessions,
        )
        event_ids.append(persisted.event_id)

    with sessions.begin() as session:
        first_outbox = session.get(DeliveryOutboxEntry, event_ids[0])
        assert first_outbox is not None
        first_outbox.published_at = first_outbox.created_at + timedelta(
            microseconds=1
        )
        session.add(
            DeliveryAttempt(
                event_id=event_ids[0],
                attempt_number=1,
                result=DeliveryAttemptResult.DELIVERED,
                response_code=202,
                latency_ms=10,
                error=None,
                started_at=LOAD_ENDED_AT - timedelta(seconds=20),
                completed_at=LOAD_ENDED_AT - timedelta(seconds=19),
            )
        )

    with sessions() as session:
        captured = capture_database_processing_observation(
            test_run_id=TEST_RUN_ID,
            load_ended_at=LOAD_ENDED_AT,
            observed_at=LOAD_ENDED_AT + timedelta(seconds=5),
            session=session,
            source_queue_visible_messages=1,
            source_queue_in_flight_messages=0,
            source_queue_delayed_messages=0,
            dead_letter_queue_messages=0,
        )

    assert captured.accepted_unique_events == 2
    assert captured.durable_outbox_entries == 2
    assert captured.completed_delivery_events == 1
    assert captured.incomplete_delivery_events == 1
    assert captured.pending_outbox_entries == 1
    assert captured.source_queue_visible_messages == 1
    assert captured.processing_drained is False
    engine.dispose()
