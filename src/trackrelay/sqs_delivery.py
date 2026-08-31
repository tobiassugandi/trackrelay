"""Amazon SQS adapters for downstream-delivery jobs and messages."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from trackrelay.services.delivery_queue import (
    DownstreamDeliveryAcknowledgementError,
    DownstreamDeliveryJob,
    DownstreamDeliveryMessageDecodeError,
    DownstreamDeliveryQueueError,
    DownstreamDeliveryReceiveError,
)


class SqsClient(Protocol):
    """Describe only the SQS operations used by TrackRelay."""

    def send_message(self, **kwargs: object) -> Mapping[str, object]: ...

    def receive_message(self, **kwargs: object) -> Mapping[str, object]: ...

    def delete_message(self, **kwargs: object) -> object: ...

    def get_queue_attributes(self, **kwargs: object) -> Mapping[str, object]: ...


def create_sqs_client(*, region_name: str) -> SqsClient:
    """Create the runtime SQS client using the standard AWS credential chain."""
    return boto3.client("sqs", region_name=region_name)


class SqsDownstreamDeliveryQueue:
    """Publish versioned downstream-delivery jobs to one SQS queue."""

    def __init__(self, *, client: SqsClient, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    def enqueue(self, job: DownstreamDeliveryJob) -> None:
        """Serialize and publish one job without exposing SQS to ingestion."""
        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=job.model_dump_json(),
            )
        except (BotoCoreError, ClientError) as error:
            raise DownstreamDeliveryQueueError(
                "SQS could not publish downstream delivery work"
            ) from error


class SqsDownstreamDeliveryMessage:
    """Adapt one raw SQS message to the worker acknowledgement boundary."""

    def __init__(
        self,
        *,
        client: SqsClient,
        queue_url: str,
        body: str,
        receipt_handle: str,
        receive_count: int,
    ) -> None:
        if receive_count < 1:
            raise ValueError("SQS receive count must be positive")
        self._client = client
        self._queue_url = queue_url
        self._body = body
        self._receipt_handle = receipt_handle
        self._receive_count = receive_count

    @property
    def job(self) -> DownstreamDeliveryJob:
        """Validate the versioned job only when the worker processes it."""
        try:
            return DownstreamDeliveryJob.model_validate_json(self._body)
        except (ValidationError, ValueError) as error:
            raise DownstreamDeliveryMessageDecodeError(
                "SQS message does not contain a supported downstream job"
            ) from error

    @property
    def receive_count(self) -> int:
        """Return SQS's approximate number of deliveries for this message."""
        return self._receive_count

    def acknowledge(self) -> None:
        """Delete this exact received message after successful processing."""
        try:
            self._client.delete_message(
                QueueUrl=self._queue_url,
                ReceiptHandle=self._receipt_handle,
            )
        except (BotoCoreError, ClientError) as error:
            raise DownstreamDeliveryAcknowledgementError(
                "SQS could not acknowledge downstream delivery work"
            ) from error


class SqsDownstreamDeliveryReceiver:
    """Long-poll one SQS queue for bounded batches of worker messages."""

    def __init__(
        self,
        *,
        client: SqsClient,
        queue_url: str,
        wait_time_seconds: int = 20,
        visibility_timeout_seconds: int = 30,
        max_messages: int = 10,
    ) -> None:
        if not 0 <= wait_time_seconds <= 20:
            raise ValueError("SQS wait time must be between 0 and 20 seconds")
        if not 1 <= visibility_timeout_seconds <= 43_200:
            raise ValueError(
                "SQS visibility timeout must be between 1 and 43200 seconds"
            )
        if not 1 <= max_messages <= 10:
            raise ValueError("SQS receive batch size must be between 1 and 10")
        self._client = client
        self._queue_url = queue_url
        self._wait_time_seconds = wait_time_seconds
        self._visibility_timeout_seconds = visibility_timeout_seconds
        self._max_messages = max_messages

    def receive(self) -> tuple[SqsDownstreamDeliveryMessage, ...]:
        """Receive and wrap at most ten messages without acknowledging them."""
        try:
            response = self._client.receive_message(
                QueueUrl=self._queue_url,
                AttributeNames=["ApproximateReceiveCount"],
                MaxNumberOfMessages=self._max_messages,
                WaitTimeSeconds=self._wait_time_seconds,
                VisibilityTimeout=self._visibility_timeout_seconds,
            )
        except (BotoCoreError, ClientError) as error:
            raise DownstreamDeliveryReceiveError(
                "SQS could not receive downstream delivery work"
            ) from error

        raw_messages = response.get("Messages", ())
        if not isinstance(raw_messages, (list, tuple)):
            raise DownstreamDeliveryReceiveError(
                "SQS receive response contained an invalid Messages field"
            )

        messages = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, Mapping):
                raise DownstreamDeliveryReceiveError(
                    "SQS receive response contained an invalid message"
                )
            body = raw_message.get("Body")
            receipt_handle = raw_message.get("ReceiptHandle")
            attributes = raw_message.get("Attributes")
            if not isinstance(body, str) or not isinstance(receipt_handle, str):
                raise DownstreamDeliveryReceiveError(
                    "SQS message omitted its body or receipt handle"
                )
            if not isinstance(attributes, Mapping):
                raise DownstreamDeliveryReceiveError(
                    "SQS message omitted its receive-count attributes"
                )
            raw_receive_count = attributes.get("ApproximateReceiveCount")
            try:
                receive_count = int(raw_receive_count)
            except (TypeError, ValueError) as error:
                raise DownstreamDeliveryReceiveError(
                    "SQS message contained an invalid receive count"
                ) from error
            if receive_count < 1:
                raise DownstreamDeliveryReceiveError(
                    "SQS message contained a non-positive receive count"
                )
            messages.append(
                SqsDownstreamDeliveryMessage(
                    client=self._client,
                    queue_url=self._queue_url,
                    body=body,
                    receipt_handle=receipt_handle,
                    receive_count=receive_count,
                )
            )
        return tuple(messages)


class SqsRedrivePolicyError(RuntimeError):
    """Report a source queue without the required retry and DLQ policy."""


@dataclass(frozen=True)
class SqsRedrivePolicy:
    """The redrive controls relevant to TrackRelay's worker."""

    dead_letter_target_arn: str
    max_receive_count: int


class SqsBacklogInspectionError(RuntimeError):
    """Report queue state that cannot support processing guardrails."""


@dataclass(frozen=True)
class SqsDeliveryBacklog:
    """Source and dead-letter queue counts relevant to drain evaluation."""

    source_visible_messages: int
    source_in_flight_messages: int
    source_delayed_messages: int
    dead_letter_queue_messages: int


def _queue_count(
    attributes: Mapping[str, object],
    attribute_name: str,
) -> int:
    raw_count = attributes.get(attribute_name)
    try:
        count = int(raw_count)
    except (TypeError, ValueError) as error:
        raise SqsBacklogInspectionError(
            f"SQS queue omitted or reported invalid {attribute_name}"
        ) from error
    if count < 0:
        raise SqsBacklogInspectionError(
            f"SQS queue reported a negative {attribute_name}"
        )
    return count


def inspect_sqs_delivery_backlog(
    *,
    client: SqsClient,
    source_queue_url: str,
    dead_letter_queue_url: str,
) -> SqsDeliveryBacklog:
    """Read visible, in-flight, delayed, and DLQ queue counts."""
    try:
        source_response = client.get_queue_attributes(
            QueueUrl=source_queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )
        dead_letter_response = client.get_queue_attributes(
            QueueUrl=dead_letter_queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
    except (BotoCoreError, ClientError) as error:
        raise SqsBacklogInspectionError(
            "SQS delivery backlog could not be inspected"
        ) from error

    source_attributes = source_response.get("Attributes")
    dead_letter_attributes = dead_letter_response.get("Attributes")
    if not isinstance(source_attributes, Mapping) or not isinstance(
        dead_letter_attributes,
        Mapping,
    ):
        raise SqsBacklogInspectionError(
            "SQS queue attributes omitted delivery backlog counts"
        )
    return SqsDeliveryBacklog(
        source_visible_messages=_queue_count(
            source_attributes,
            "ApproximateNumberOfMessages",
        ),
        source_in_flight_messages=_queue_count(
            source_attributes,
            "ApproximateNumberOfMessagesNotVisible",
        ),
        source_delayed_messages=_queue_count(
            source_attributes,
            "ApproximateNumberOfMessagesDelayed",
        ),
        dead_letter_queue_messages=_queue_count(
            dead_letter_attributes,
            "ApproximateNumberOfMessages",
        ),
    )


def verify_sqs_redrive_policy(
    *,
    client: SqsClient,
    queue_url: str,
    expected_dead_letter_target_arn: str,
    expected_max_receive_count: int,
) -> SqsRedrivePolicy:
    """Fail worker startup unless SQS owns the expected retry-to-DLQ policy."""
    if expected_max_receive_count < 1:
        raise ValueError("expected SQS max receive count must be positive")
    try:
        response = client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["RedrivePolicy"],
        )
    except (BotoCoreError, ClientError) as error:
        raise SqsRedrivePolicyError(
            "SQS redrive policy could not be inspected"
        ) from error

    attributes = response.get("Attributes")
    if not isinstance(attributes, Mapping):
        raise SqsRedrivePolicyError(
            "SQS queue attributes omitted the redrive policy"
        )
    raw_policy = attributes.get("RedrivePolicy")
    if not isinstance(raw_policy, str):
        raise SqsRedrivePolicyError("SQS queue has no redrive policy")
    try:
        decoded_policy = json.loads(raw_policy)
        dead_letter_target_arn = decoded_policy["deadLetterTargetArn"]
        max_receive_count = int(decoded_policy["maxReceiveCount"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SqsRedrivePolicyError(
            "SQS queue has an invalid redrive policy"
        ) from error
    if not isinstance(dead_letter_target_arn, str) or not dead_letter_target_arn:
        raise SqsRedrivePolicyError(
            "SQS queue has an invalid dead-letter target ARN"
        )
    policy = SqsRedrivePolicy(
        dead_letter_target_arn=dead_letter_target_arn,
        max_receive_count=max_receive_count,
    )
    if policy.dead_letter_target_arn != expected_dead_letter_target_arn:
        raise SqsRedrivePolicyError(
            "SQS redrive policy targets an unexpected dead-letter queue"
        )
    if policy.max_receive_count != expected_max_receive_count:
        raise SqsRedrivePolicyError(
            "SQS redrive policy has an unexpected max receive count"
        )
    return policy
