"""Tests that lock down the documented ingestion response contracts."""

from uuid import uuid4

from pydantic import ValidationError
from pytest import raises

from trackrelay.main import DuplicateEventResponse, QueuedEventResponse


def test_created_response_reports_queued_work_without_claiming_delivery() -> None:
    event_id = uuid4()

    response = QueuedEventResponse(
        event_id=event_id,
        processing_status="processed",
        duplicate=False,
        delivery_status="queued",
        downstream_status_code=None,
    )

    assert response.model_dump(mode="json") == {
        "event_id": str(event_id),
        "processing_status": "processed",
        "duplicate": False,
        "delivery_status": "queued",
        "downstream_status_code": None,
    }


def test_duplicate_response_identifies_the_original_event_and_skipped_delivery() -> None:
    original_event_id = uuid4()

    response = DuplicateEventResponse(
        event_id=original_event_id,
        processing_status="processed",
        duplicate=True,
        delivery_status="skipped_duplicate",
        downstream_status_code=None,
    )

    assert response.model_dump(mode="json") == {
        "event_id": str(original_event_id),
        "processing_status": "processed",
        "duplicate": True,
        "delivery_status": "skipped_duplicate",
        "downstream_status_code": None,
    }


def test_duplicate_response_cannot_claim_another_delivery() -> None:
    with raises(ValidationError):
        DuplicateEventResponse.model_validate(
            {
                "event_id": str(uuid4()),
                "processing_status": "processed",
                "duplicate": True,
                "delivery_status": "delivered",
                "downstream_status_code": 202,
            }
        )
