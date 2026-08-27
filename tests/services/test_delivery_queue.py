"""Tests for the application-facing downstream queue boundary."""

from uuid import UUID

from pydantic import ValidationError
from pytest import raises

from trackrelay.services import DownstreamDeliveryJob, DownstreamDeliveryQueue


class RecordingDownstreamDeliveryQueue:
    """Deterministic test fake that records enqueue calls in call order."""

    def __init__(self) -> None:
        self._enqueued_jobs: list[DownstreamDeliveryJob] = []

    @property
    def enqueued_jobs(self) -> tuple[DownstreamDeliveryJob, ...]:
        return tuple(self._enqueued_jobs)

    def enqueue(self, job: DownstreamDeliveryJob) -> None:
        self._enqueued_jobs.append(job)


def test_delivery_job_has_a_small_versioned_transport_shape() -> None:
    event_id = UUID("00000000-0000-0000-0000-000000000901")

    job = DownstreamDeliveryJob(event_id=event_id)

    assert job.model_dump(mode="json") == {
        "schema_version": 1,
        "event_id": str(event_id),
    }


def test_delivery_job_rejects_fields_outside_its_transport_contract() -> None:
    with raises(ValidationError):
        DownstreamDeliveryJob.model_validate(
            {
                "event_id": "00000000-0000-0000-0000-000000000901",
                "normalized_event": {"status": "delivered"},
            }
        )


def test_recording_fake_implements_the_queue_contract_without_hiding_duplicates(
) -> None:
    queue = RecordingDownstreamDeliveryQueue()
    job = DownstreamDeliveryJob(
        event_id=UUID("00000000-0000-0000-0000-000000000901")
    )

    queue.enqueue(job)
    queue.enqueue(job)

    assert isinstance(queue, DownstreamDeliveryQueue)
    assert queue.enqueued_jobs == (job, job)
