"""Tests for the real SQS adapters without making AWS calls."""

import json
from collections.abc import Mapping
from uuid import UUID

from botocore.exceptions import EndpointConnectionError
from pytest import raises

from trackrelay.domain import NormalizedEvent, ShipmentStatus
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryAcknowledgementError,
    DownstreamDeliveryJob,
    DownstreamDeliveryMessage,
    DownstreamDeliveryMessageDecodeError,
    DownstreamDeliveryQueue,
    DownstreamDeliveryQueueError,
    DownstreamDeliveryReceiveError,
    DownstreamDeliveryReceiver,
)
from trackrelay.sqs_delivery import (
    SqsDownstreamDeliveryQueue,
    SqsDownstreamDeliveryReceiver,
    SqsRedrivePolicy,
    SqsRedrivePolicyError,
    verify_sqs_redrive_policy,
)
from trackrelay.worker import WorkerBatchResult, process_worker_batch

QUEUE_URL = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"
EVENT_ID = UUID("00000000-0000-0000-0000-000000000942")


class FakeSqsClient:
    """Record SQS requests and return configured deterministic responses."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.receives: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.receive_response: Mapping[str, object] = {}
        self.queue_attributes_response: Mapping[str, object] = {}
        self.send_error: Exception | None = None
        self.receive_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.queue_attributes_error: Exception | None = None
        self.queue_attribute_requests: list[dict[str, object]] = []

    def send_message(self, **kwargs: object) -> Mapping[str, object]:
        self.sent.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return {"MessageId": "message-1"}

    def receive_message(self, **kwargs: object) -> Mapping[str, object]:
        self.receives.append(kwargs)
        if self.receive_error is not None:
            raise self.receive_error
        return self.receive_response

    def delete_message(self, **kwargs: object) -> Mapping[str, object]:
        self.deletes.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def get_queue_attributes(self, **kwargs: object) -> Mapping[str, object]:
        self.queue_attribute_requests.append(kwargs)
        if self.queue_attributes_error is not None:
            raise self.queue_attributes_error
        return self.queue_attributes_response


def endpoint_error() -> EndpointConnectionError:
    return EndpointConnectionError(endpoint_url=QUEUE_URL)


def test_sqs_publisher_sends_only_the_versioned_job() -> None:
    client = FakeSqsClient()
    queue = SqsDownstreamDeliveryQueue(client=client, queue_url=QUEUE_URL)

    queue.enqueue(DownstreamDeliveryJob(event_id=EVENT_ID))

    assert isinstance(queue, DownstreamDeliveryQueue)
    assert client.sent == [
        {
            "QueueUrl": QUEUE_URL,
            "MessageBody": (
                '{"schema_version":1,"event_id":'
                '"00000000-0000-0000-0000-000000000942"}'
            ),
        }
    ]


def test_sqs_publisher_translates_aws_failure_to_the_application_boundary() -> None:
    client = FakeSqsClient()
    client.send_error = endpoint_error()
    queue = SqsDownstreamDeliveryQueue(client=client, queue_url=QUEUE_URL)

    with raises(DownstreamDeliveryQueueError, match="could not publish"):
        queue.enqueue(DownstreamDeliveryJob(event_id=EVENT_ID))


def test_sqs_receiver_long_polls_and_acknowledges_the_exact_receipt() -> None:
    client = FakeSqsClient()
    job = DownstreamDeliveryJob(event_id=EVENT_ID)
    client.receive_response = {
        "Messages": [
            {
                "Body": job.model_dump_json(),
                "ReceiptHandle": "receipt-1",
                "Attributes": {"ApproximateReceiveCount": "3"},
            }
        ]
    }
    receiver = SqsDownstreamDeliveryReceiver(
        client=client,
        queue_url=QUEUE_URL,
        wait_time_seconds=15,
        visibility_timeout_seconds=60,
        max_messages=7,
    )

    messages = receiver.receive()

    assert isinstance(receiver, DownstreamDeliveryReceiver)
    assert client.receives == [
        {
            "QueueUrl": QUEUE_URL,
            "AttributeNames": ["ApproximateReceiveCount"],
            "MaxNumberOfMessages": 7,
            "WaitTimeSeconds": 15,
            "VisibilityTimeout": 60,
        }
    ]
    assert len(messages) == 1
    assert isinstance(messages[0], DownstreamDeliveryMessage)
    assert messages[0].job == job
    assert messages[0].receive_count == 3
    assert client.deletes == []

    messages[0].acknowledge()

    assert client.deletes == [
        {"QueueUrl": QUEUE_URL, "ReceiptHandle": "receipt-1"}
    ]


def test_sqs_message_rejects_an_invalid_job_without_acknowledging() -> None:
    client = FakeSqsClient()
    client.receive_response = {
        "Messages": [
            {
                "Body": '{"schema_version":2,"event_id":"not-a-uuid"}',
                "ReceiptHandle": "receipt-invalid",
                "Attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
    message = SqsDownstreamDeliveryReceiver(
        client=client,
        queue_url=QUEUE_URL,
    ).receive()[0]

    with raises(DownstreamDeliveryMessageDecodeError, match="supported"):
        _ = message.job

    assert client.deletes == []


def test_sqs_receive_and_acknowledgement_failures_are_explicit() -> None:
    client = FakeSqsClient()
    client.receive_error = endpoint_error()
    receiver = SqsDownstreamDeliveryReceiver(
        client=client,
        queue_url=QUEUE_URL,
    )

    with raises(DownstreamDeliveryReceiveError, match="could not receive"):
        receiver.receive()

    client.receive_error = None
    client.receive_response = {
        "Messages": [
            {
                "Body": DownstreamDeliveryJob(
                    event_id=EVENT_ID
                ).model_dump_json(),
                "ReceiptHandle": "receipt-delete-fails",
                "Attributes": {"ApproximateReceiveCount": "2"},
            }
        ]
    }
    client.delete_error = endpoint_error()
    message = receiver.receive()[0]

    with raises(
        DownstreamDeliveryAcknowledgementError,
        match="could not acknowledge",
    ):
        message.acknowledge()


def test_sqs_receiver_rejects_invalid_configuration_and_response_shapes() -> None:
    client = FakeSqsClient()
    with raises(ValueError, match="wait time"):
        SqsDownstreamDeliveryReceiver(
            client=client,
            queue_url=QUEUE_URL,
            wait_time_seconds=21,
        )
    with raises(ValueError, match="batch size"):
        SqsDownstreamDeliveryReceiver(
            client=client,
            queue_url=QUEUE_URL,
            max_messages=11,
        )

    client.receive_response = {"Messages": "not-a-list"}
    receiver = SqsDownstreamDeliveryReceiver(
        client=client,
        queue_url=QUEUE_URL,
    )
    with raises(DownstreamDeliveryReceiveError, match="Messages field"):
        receiver.receive()

    client.receive_response = {
        "Messages": [
            {
                "Body": DownstreamDeliveryJob(
                    event_id=EVENT_ID
                ).model_dump_json(),
                "ReceiptHandle": "receipt-no-count",
            }
        ]
    }
    with raises(DownstreamDeliveryReceiveError, match="receive-count"):
        receiver.receive()


def test_malformed_sqs_message_does_not_block_a_valid_neighbor() -> None:
    client = FakeSqsClient()
    valid_job = DownstreamDeliveryJob(event_id=EVENT_ID)
    client.receive_response = {
        "Messages": [
            {
                "Body": "not-json",
                "ReceiptHandle": "receipt-invalid",
                "Attributes": {"ApproximateReceiveCount": "5"},
            },
            {
                "Body": valid_job.model_dump_json(),
                "ReceiptHandle": "receipt-valid",
                "Attributes": {"ApproximateReceiveCount": "1"},
            },
        ]
    }
    messages = SqsDownstreamDeliveryReceiver(
        client=client,
        queue_url=QUEUE_URL,
    ).receive()
    errors: list[tuple[int, Exception]] = []

    result = process_worker_batch(
        messages,
        load_event=lambda event_id: NormalizedEvent.model_validate(
            {
                "partner_id": "courier-alpha",
                "partner_event_id": "SQS-VALID-001",
                "tracking_number": "SQS-TRK-001",
                "status": ShipmentStatus.PICKED_UP,
                "occurred_at": "2026-08-31T05:00:00Z",
                "received_at": "2026-08-31T05:00:01Z",
                "raw_payload": {"status": "PICKUP"},
            }
        ),
        deliver_and_record_event=lambda event, event_id: DeliveryResult(
            downstream_status_code=202
        ),
        load_recorded_delivery=lambda event_id: None,
        on_processing_error=lambda message, error: errors.append(
            (message.receive_count, error)
        ),
    )

    assert result == WorkerBatchResult(
        received=2,
        delivered_and_acknowledged=1,
        failed_and_unacknowledged=1,
    )
    assert len(errors) == 1
    assert errors[0][0] == 5
    assert isinstance(errors[0][1], DownstreamDeliveryMessageDecodeError)
    assert client.deletes == [
        {"QueueUrl": QUEUE_URL, "ReceiptHandle": "receipt-valid"}
    ]


def test_worker_startup_verifies_the_expected_sqs_redrive_policy() -> None:
    client = FakeSqsClient()
    dlq_arn = "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq"
    client.queue_attributes_response = {
        "Attributes": {
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": dlq_arn,
                    "maxReceiveCount": "5",
                }
            )
        }
    }

    policy = verify_sqs_redrive_policy(
        client=client,
        queue_url=QUEUE_URL,
        expected_dead_letter_target_arn=dlq_arn,
        expected_max_receive_count=5,
    )

    assert policy == SqsRedrivePolicy(
        dead_letter_target_arn=dlq_arn,
        max_receive_count=5,
    )
    assert client.queue_attribute_requests == [
        {"QueueUrl": QUEUE_URL, "AttributeNames": ["RedrivePolicy"]}
    ]


def test_worker_startup_rejects_missing_or_mismatched_redrive_policy() -> None:
    client = FakeSqsClient()
    dlq_arn = "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq"

    with raises(SqsRedrivePolicyError, match="omitted"):
        verify_sqs_redrive_policy(
            client=client,
            queue_url=QUEUE_URL,
            expected_dead_letter_target_arn=dlq_arn,
            expected_max_receive_count=5,
        )

    client.queue_attributes_response = {
        "Attributes": {
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": f"{dlq_arn}-wrong",
                    "maxReceiveCount": "3",
                }
            )
        }
    }
    with raises(SqsRedrivePolicyError, match="unexpected dead-letter"):
        verify_sqs_redrive_policy(
            client=client,
            queue_url=QUEUE_URL,
            expected_dead_letter_target_arn=dlq_arn,
            expected_max_receive_count=5,
        )

    client.queue_attributes_response = {
        "Attributes": {
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": dlq_arn,
                    "maxReceiveCount": "3",
                }
            )
        }
    }
    with raises(SqsRedrivePolicyError, match="unexpected max receive"):
        verify_sqs_redrive_policy(
            client=client,
            queue_url=QUEUE_URL,
            expected_dead_letter_target_arn=dlq_arn,
            expected_max_receive_count=5,
        )
