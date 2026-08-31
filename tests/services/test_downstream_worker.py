"""Tests for transport-neutral worker delivery and acknowledgement behavior."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pytest import raises

from trackrelay.database import Base, create_database_engine, create_session_factory
from trackrelay.domain import EventProcessingStatus, NormalizedEvent, ShipmentStatus
from trackrelay.models import Event, Partner, Shipment
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryJob,
    PersistedEventNotFoundError,
    RecordingDownstreamDeliveryMessage,
    load_persisted_normalized_event,
    process_downstream_delivery_message,
)

EVENT_ID = UUID("00000000-0000-0000-0000-000000000941")
OCCURRED_AT = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)


def normalized_event() -> NormalizedEvent:
    return NormalizedEvent(
        partner_id="courier-alpha",
        partner_event_id="WORKER-EVENT-001",
        tracking_number="WORKER-TRK-001",
        status=ShipmentStatus.PICKED_UP,
        occurred_at=OCCURRED_AT,
        received_at=OCCURRED_AT + timedelta(seconds=2),
        raw_payload={"status": "PICKUP"},
    )


def test_worker_loads_delivers_records_then_acknowledges() -> None:
    expected_event = normalized_event()
    actions: list[str] = []
    message = RecordingDownstreamDeliveryMessage(
        DownstreamDeliveryJob(event_id=EVENT_ID),
        on_acknowledge=lambda: actions.append("acknowledge"),
    )

    def load_event(event_id: UUID) -> NormalizedEvent:
        assert event_id == EVENT_ID
        actions.append("load")
        return expected_event

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        assert event == expected_event
        assert event_id == EVENT_ID
        assert message.acknowledgement_calls == 0
        actions.append("deliver_and_record")
        return DeliveryResult(downstream_status_code=202)

    result = process_downstream_delivery_message(
        message,
        load_event=load_event,
        deliver_and_record_event=deliver_and_record,
    )

    assert result == DeliveryResult(downstream_status_code=202)
    assert actions == ["load", "deliver_and_record", "acknowledge"]
    assert message.acknowledgement_calls == 1
    assert message.acknowledged is True


def test_worker_leaves_message_unacknowledged_when_loading_fails() -> None:
    message = RecordingDownstreamDeliveryMessage(
        DownstreamDeliveryJob(event_id=EVENT_ID)
    )
    deliveries = 0

    def missing_event(event_id: UUID) -> NormalizedEvent:
        raise PersistedEventNotFoundError(str(event_id))

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        nonlocal deliveries
        deliveries += 1
        return DeliveryResult(downstream_status_code=202)

    with raises(PersistedEventNotFoundError):
        process_downstream_delivery_message(
            message,
            load_event=missing_event,
            deliver_and_record_event=deliver_and_record,
        )

    assert deliveries == 0
    assert message.acknowledgement_calls == 0
    assert message.acknowledged is False


def test_worker_leaves_message_unacknowledged_when_delivery_fails() -> None:
    message = RecordingDownstreamDeliveryMessage(
        DownstreamDeliveryJob(event_id=EVENT_ID)
    )

    def fail_delivery(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        raise RuntimeError("recorded downstream failure")

    with raises(RuntimeError, match="recorded downstream failure"):
        process_downstream_delivery_message(
            message,
            load_event=lambda event_id: normalized_event(),
            deliver_and_record_event=fail_delivery,
        )

    assert message.acknowledgement_calls == 0
    assert message.acknowledged is False


def test_worker_exposes_acknowledgement_failure_after_delivery() -> None:
    acknowledgement_error = RuntimeError("delete failed")
    message = RecordingDownstreamDeliveryMessage(
        DownstreamDeliveryJob(event_id=EVENT_ID),
        acknowledgement_failure=acknowledgement_error,
    )
    deliveries = 0

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        nonlocal deliveries
        deliveries += 1
        return DeliveryResult(downstream_status_code=202)

    with raises(RuntimeError, match="delete failed"):
        process_downstream_delivery_message(
            message,
            load_event=lambda event_id: normalized_event(),
            deliver_and_record_event=deliver_and_record,
        )

    assert deliveries == 1
    assert message.acknowledgement_calls == 1
    assert message.acknowledged is False


def test_worker_loader_reconstructs_the_authoritative_persisted_event() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    expected_event = normalized_event()

    with sessions.begin() as session:
        session.add(
            Partner(
                id=expected_event.partner_id,
                name="Courier Alpha",
                adapter_type="courier-alpha",
            )
        )
        session.add(
            Shipment(
                tracking_number=expected_event.tracking_number,
                current_status=expected_event.status,
                current_status_occurred_at=expected_event.occurred_at,
            )
        )
        session.flush()
        event = Event(
            partner_id=expected_event.partner_id,
            partner_event_id=expected_event.partner_event_id,
            tracking_number=expected_event.tracking_number,
            status=expected_event.status,
            occurred_at=expected_event.occurred_at,
            received_at=expected_event.received_at,
            raw_payload=expected_event.raw_payload,
            processing_status=EventProcessingStatus.PROCESSED,
            state_applied=True,
        )
        session.add(event)
        session.flush()
        event_id = event.id

    loaded_event = load_persisted_normalized_event(event_id, sessions=sessions)

    assert loaded_event == expected_event
    with raises(PersistedEventNotFoundError, match="does not exist"):
        load_persisted_normalized_event(EVENT_ID, sessions=sessions)
    engine.dispose()
