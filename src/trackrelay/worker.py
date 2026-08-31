"""Run downstream delivery in a process separate from ingestion."""

import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from trackrelay.config import Settings
from trackrelay.domain import NormalizedEvent
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryMessage,
    DownstreamDeliveryQueueError,
    DownstreamDeliveryReceiver,
    deliver_and_record_normalized_event,
    process_downstream_delivery_message,
    publish_pending_delivery_jobs,
)
from trackrelay.services.downstream_worker import (
    PersistedEventLoader,
    RecordedDeliveryLoader,
    RecordedEventDeliverer,
    load_persisted_normalized_event,
    load_recorded_delivery_result,
)
from trackrelay.sqs_delivery import (
    SqsDownstreamDeliveryQueue,
    SqsDownstreamDeliveryReceiver,
    create_sqs_client,
    verify_sqs_redrive_policy,
)

logger = logging.getLogger(__name__)
ProcessingErrorHandler = Callable[[DownstreamDeliveryMessage, Exception], None]
OutboxPublishingErrorHandler = Callable[[Exception], None]
PendingDeliveryPublisher = Callable[[], int]
StopRequested = Callable[[], bool]


@dataclass(frozen=True)
class WorkerBatchResult:
    """Summarize one received batch without hiding failed messages."""

    received: int
    delivered_and_acknowledged: int
    failed_and_unacknowledged: int


def log_processing_error(
    message: DownstreamDeliveryMessage,
    error: Exception,
) -> None:
    """Record one failed message while allowing the batch to continue."""
    logger.error(
        "downstream delivery message failed and remains unacknowledged",
        extra={"receive_count": message.receive_count},
        exc_info=(type(error), error, error.__traceback__),
    )


def log_outbox_publishing_error(error: Exception) -> None:
    """Record a recoverable relay failure before the next polling cycle."""
    logger.error(
        "delivery outbox publication failed; entries remain pending",
        exc_info=(type(error), error, error.__traceback__),
    )


def process_worker_batch(
    messages: Sequence[DownstreamDeliveryMessage],
    *,
    deliver_and_record_event: RecordedEventDeliverer,
    load_event: PersistedEventLoader = load_persisted_normalized_event,
    load_recorded_delivery: RecordedDeliveryLoader = load_recorded_delivery_result,
    on_processing_error: ProcessingErrorHandler = log_processing_error,
) -> WorkerBatchResult:
    """Process every message once and isolate failures within the batch."""
    delivered = 0
    failed = 0
    for message in messages:
        try:
            process_downstream_delivery_message(
                message,
                deliver_and_record_event=deliver_and_record_event,
                load_event=load_event,
                load_recorded_delivery=load_recorded_delivery,
            )
        # One poison or failed message must not prevent the rest of the batch.
        except Exception as error:  # noqa: BLE001
            failed += 1
            on_processing_error(message, error)
        else:
            delivered += 1
    return WorkerBatchResult(
        received=len(messages),
        delivered_and_acknowledged=delivered,
        failed_and_unacknowledged=failed,
    )


def run_worker(
    receiver: DownstreamDeliveryReceiver,
    *,
    deliver_and_record_event: RecordedEventDeliverer,
    stop_requested: StopRequested,
    publish_pending_deliveries: PendingDeliveryPublisher | None = None,
    load_event: PersistedEventLoader = load_persisted_normalized_event,
    load_recorded_delivery: RecordedDeliveryLoader = load_recorded_delivery_result,
    on_outbox_publishing_error: OutboxPublishingErrorHandler = (
        log_outbox_publishing_error
    ),
    on_processing_error: ProcessingErrorHandler = log_processing_error,
) -> None:
    """Long-poll and process batches until the process is asked to stop."""
    while not stop_requested():
        if publish_pending_deliveries is not None:
            try:
                publish_pending_deliveries()
            except (DownstreamDeliveryQueueError, SQLAlchemyError) as error:
                on_outbox_publishing_error(error)
        messages = receiver.receive()
        process_worker_batch(
            messages,
            deliver_and_record_event=deliver_and_record_event,
            load_event=load_event,
            load_recorded_delivery=load_recorded_delivery,
            on_processing_error=on_processing_error,
        )


def main() -> int:
    """Compose the SQS worker from environment-backed runtime settings."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.delivery_queue_backend != "sqs" or not settings.sqs_queue_url:
        raise SystemExit(
            "TRACKRELAY_DELIVERY_QUEUE_BACKEND=sqs and "
            "TRACKRELAY_SQS_QUEUE_URL are required by the worker"
        )

    sqs_client = create_sqs_client(region_name=settings.aws_region)
    assert settings.sqs_dead_letter_queue_arn is not None
    verify_sqs_redrive_policy(
        client=sqs_client,
        queue_url=settings.sqs_queue_url,
        expected_dead_letter_target_arn=settings.sqs_dead_letter_queue_arn,
        expected_max_receive_count=settings.sqs_max_receive_count,
    )
    delivery_queue = SqsDownstreamDeliveryQueue(
        client=sqs_client,
        queue_url=settings.sqs_queue_url,
    )
    receiver = SqsDownstreamDeliveryReceiver(
        client=sqs_client,
        queue_url=settings.sqs_queue_url,
        wait_time_seconds=settings.sqs_wait_time_seconds,
        visibility_timeout_seconds=settings.sqs_visibility_timeout_seconds,
        max_messages=settings.sqs_max_messages,
    )

    def deliver_and_record(
        event: NormalizedEvent,
        event_id: UUID,
    ) -> DeliveryResult:
        return deliver_and_record_normalized_event(
            event,
            event_id=event_id,
            downstream_url=settings.downstream_url,
            timeout_seconds=settings.downstream_timeout_seconds,
        )

    stop_event = Event()

    def publish_pending_deliveries() -> int:
        return publish_pending_delivery_jobs(
            delivery_queue,
            batch_size=settings.sqs_max_messages,
        )

    def request_stop(signum: int, frame: object) -> None:
        logger.info("worker stop requested", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_worker(
        receiver,
        deliver_and_record_event=deliver_and_record,
        stop_requested=stop_event.is_set,
        publish_pending_deliveries=publish_pending_deliveries,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
