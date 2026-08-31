"""Tests for the separately runnable downstream worker loop."""

from collections.abc import Sequence
from uuid import UUID

from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryJob,
    DownstreamDeliveryMessage,
    RecordingDownstreamDeliveryMessage,
)
from trackrelay.worker import WorkerBatchResult, process_worker_batch, run_worker

EVENT_IDS = (
    UUID("00000000-0000-0000-0000-000000000951"),
    UUID("00000000-0000-0000-0000-000000000952"),
    UUID("00000000-0000-0000-0000-000000000953"),
)


def event_for(event_id: UUID) -> NormalizedEvent:
    return NormalizedEvent.model_validate(
        {
            "partner_id": "courier-alpha",
            "partner_event_id": f"WORKER-{event_id}",
            "tracking_number": f"TRK-{event_id}",
            "status": ShipmentStatus.PICKED_UP,
            "occurred_at": "2026-08-31T05:00:00Z",
            "received_at": "2026-08-31T05:00:01Z",
            "raw_payload": {"status": "PICKUP"},
        }
    )


def test_worker_batch_isolates_a_failed_message_and_continues() -> None:
    messages = tuple(
        RecordingDownstreamDeliveryMessage(
            DownstreamDeliveryJob(event_id=event_id),
            receive_count=index,
        )
        for index, event_id in enumerate(EVENT_IDS, start=1)
    )
    delivered: list[UUID] = []
    errors: list[tuple[int, Exception]] = []

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        if event_id == EVENT_IDS[1]:
            raise RuntimeError("simulated delivery failure")
        delivered.append(event_id)
        return DeliveryResult(downstream_status_code=202)

    result = process_worker_batch(
        messages,
        load_event=event_for,
        load_recorded_delivery=lambda event_id: None,
        deliver_and_record_event=deliver_and_record,
        on_processing_error=lambda message, error: errors.append(
            (message.receive_count, error)
        ),
    )

    assert result == WorkerBatchResult(
        received=3,
        delivered_and_acknowledged=2,
        failed_and_unacknowledged=1,
    )
    assert delivered == [EVENT_IDS[0], EVENT_IDS[2]]
    assert [message.acknowledged for message in messages] == [True, False, True]
    assert len(errors) == 1
    assert errors[0][0] == 2
    assert str(errors[0][1]) == "simulated delivery failure"


class RecordingReceiver:
    """Return configured batches and record each worker poll."""

    def __init__(
        self,
        batches: Sequence[Sequence[DownstreamDeliveryMessage]],
    ) -> None:
        self._batches = iter(batches)
        self.receive_calls = 0

    def receive(self) -> tuple[DownstreamDeliveryMessage, ...]:
        self.receive_calls += 1
        return tuple(next(self._batches))


def test_worker_loop_polls_until_stop_is_requested() -> None:
    messages = tuple(
        RecordingDownstreamDeliveryMessage(
            DownstreamDeliveryJob(event_id=event_id)
        )
        for event_id in EVENT_IDS[:2]
    )
    receiver = RecordingReceiver(((messages[0],), (messages[1],)))
    delivered: list[UUID] = []

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        delivered.append(event_id)
        return DeliveryResult(downstream_status_code=202)

    run_worker(
        receiver,
        deliver_and_record_event=deliver_and_record,
        stop_requested=lambda: receiver.receive_calls == 2,
        load_event=event_for,
        load_recorded_delivery=lambda event_id: None,
        on_processing_error=lambda message, error: None,
    )

    assert receiver.receive_calls == 2
    assert delivered == list(EVENT_IDS[:2])
    assert all(message.acknowledged for message in messages)
