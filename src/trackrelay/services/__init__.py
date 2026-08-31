"""Application services coordinating domain and persistence work."""

from trackrelay.services.delivery_queue import (
    DownstreamDeliveryAcknowledgementError,
    DownstreamDeliveryJob,
    DownstreamDeliveryMessage,
    DownstreamDeliveryMessageDecodeError,
    DownstreamDeliveryQueue,
    DownstreamDeliveryQueueError,
    DownstreamDeliveryReceiveError,
    DownstreamDeliveryReceiver,
    RecordingDownstreamDeliveryMessage,
    RecordingDownstreamDeliveryQueue,
)
from trackrelay.services.downstream_delivery import (
    DeliveryResult,
    deliver_and_record_normalized_event,
    deliver_normalized_event,
    record_delivery_attempt,
)
from trackrelay.services.downstream_worker import (
    PersistedEventNotFoundError,
    load_persisted_normalized_event,
    process_downstream_delivery_message,
)
from trackrelay.services.event_persistence import (
    EventPersistenceResult,
    persist_normalized_event,
)
from trackrelay.services.shipment_history import list_shipment_events

__all__ = [
    "DeliveryResult",
    "DownstreamDeliveryAcknowledgementError",
    "DownstreamDeliveryJob",
    "DownstreamDeliveryMessage",
    "DownstreamDeliveryMessageDecodeError",
    "DownstreamDeliveryQueue",
    "DownstreamDeliveryQueueError",
    "DownstreamDeliveryReceiveError",
    "DownstreamDeliveryReceiver",
    "EventPersistenceResult",
    "PersistedEventNotFoundError",
    "RecordingDownstreamDeliveryMessage",
    "RecordingDownstreamDeliveryQueue",
    "deliver_and_record_normalized_event",
    "deliver_normalized_event",
    "list_shipment_events",
    "load_persisted_normalized_event",
    "persist_normalized_event",
    "process_downstream_delivery_message",
    "record_delivery_attempt",
]
