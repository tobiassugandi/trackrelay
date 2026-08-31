"""Amazon SQS adapters for downstream-delivery jobs and messages."""

from collections.abc import Mapping
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
    ) -> None:
        self._client = client
        self._queue_url = queue_url
        self._body = body
        self._receipt_handle = receipt_handle

    @property
    def job(self) -> DownstreamDeliveryJob:
        """Validate the versioned job only when the worker processes it."""
        try:
            return DownstreamDeliveryJob.model_validate_json(self._body)
        except (ValidationError, ValueError) as error:
            raise DownstreamDeliveryMessageDecodeError(
                "SQS message does not contain a supported downstream job"
            ) from error

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
            if not isinstance(body, str) or not isinstance(receipt_handle, str):
                raise DownstreamDeliveryReceiveError(
                    "SQS message omitted its body or receipt handle"
                )
            messages.append(
                SqsDownstreamDeliveryMessage(
                    client=self._client,
                    queue_url=self._queue_url,
                    body=body,
                    receipt_handle=receipt_handle,
                )
            )
        return tuple(messages)
