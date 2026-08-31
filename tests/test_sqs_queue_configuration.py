"""Tests for selecting the real SQS publisher at the API boundary."""

from collections.abc import Mapping
from uuid import UUID

from pytest import MonkeyPatch

import trackrelay.main as api_module
from trackrelay.config import Settings
from trackrelay.services import DownstreamDeliveryJob
from trackrelay.sqs_delivery import SqsDownstreamDeliveryQueue

QUEUE_URL = "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"


class PublishingClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send_message(self, **kwargs: object) -> Mapping[str, object]:
        self.sent.append(kwargs)
        return {"MessageId": "message-1"}

    def receive_message(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("API publisher must not receive messages")

    def delete_message(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("API publisher must not delete messages")


def test_api_selects_and_reuses_the_configured_sqs_publisher(
    monkeypatch: MonkeyPatch,
) -> None:
    client = PublishingClient()
    configured_settings = Settings(
        _env_file=None,
        delivery_queue_backend="sqs",
        sqs_queue_url=QUEUE_URL,
        aws_region="ap-southeast-3",
    )
    monkeypatch.setattr(api_module, "settings", configured_settings)
    monkeypatch.setattr(
        api_module,
        "create_sqs_client",
        lambda *, region_name: client,
    )
    api_module.get_sqs_downstream_delivery_queue.cache_clear()
    try:
        first = api_module.get_downstream_delivery_queue()
        second = api_module.get_downstream_delivery_queue()
        first.enqueue(
            DownstreamDeliveryJob(
                event_id=UUID("00000000-0000-0000-0000-000000000943")
            )
        )

        assert isinstance(first, SqsDownstreamDeliveryQueue)
        assert second is first
        assert client.sent[0]["QueueUrl"] == QUEUE_URL
    finally:
        api_module.get_sqs_downstream_delivery_queue.cache_clear()
