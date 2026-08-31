"""Run downstream delivery in a process separate from ingestion."""

import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event
from uuid import UUID

from trackrelay.config import Settings
from trackrelay.domain import NormalizedEvent
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryMessage,
    DownstreamDeliveryReceiver,
    deliver_and_record_normalized_event,
    process_downstream_delivery_message,
)
from trackrelay.services.downstream_worker import (
    PersistedEventLoader,
    RecordedEventDeliverer,
    load_persisted_normalized_event,
)
from trackrelay.sqs_delivery import (
    SqsDownstreamDeliveryReceiver,
    create_sqs_client,
)

logger = logging.getLogger(__name__)
ProcessingErrorHandler = Callable[[Exception], None]
StopRequested = Callable[[], bool]


@dataclass(frozen=True)
class WorkerBatchResult:
    """Summarize one received batch without hiding failed messages."""

    received: int
    delivered_and_acknowledged: int
    failed_and_unacknowledged: int


def log_processing_error(error: Exception) -> None:
    """Record one failed message while allowing the batch to continue."""
    logger.error(
        "downstream delivery message failed and remains unacknowledged",
        exc_info=(type(error), error, error.__traceback__),
    )


def process_worker_batch(
    messages: Sequence[DownstreamDeliveryMessage],
    *,
    deliver_and_record_event: RecordedEventDeliverer,
    load_event: PersistedEventLoader = load_persisted_normalized_event,
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
            )
        # One poison or failed message must not prevent the rest of the batch.
        except Exception as error:  # noqa: BLE001
            failed += 1
            on_processing_error(error)
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
    load_event: PersistedEventLoader = load_persisted_normalized_event,
    on_processing_error: ProcessingErrorHandler = log_processing_error,
) -> None:
    """Long-poll and process batches until the process is asked to stop."""
    while not stop_requested():
        messages = receiver.receive()
        process_worker_batch(
            messages,
            deliver_and_record_event=deliver_and_record_event,
            load_event=load_event,
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

    def request_stop(signum: int, frame: object) -> None:
        logger.info("worker stop requested", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_worker(
        receiver,
        deliver_and_record_event=deliver_and_record,
        stop_requested=stop_event.is_set,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
