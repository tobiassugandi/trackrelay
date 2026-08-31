"""PostgreSQL-backed end-to-end Courier Alpha scenarios."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from pytest import fixture, mark
from sqlalchemy import delete, func, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import (
    DeliveryAttemptResult,
    EventProcessingStatus,
    NormalizedEvent,
    ShipmentStatus,
)
from trackrelay.downstream.main import app as downstream_app
from trackrelay.downstream.main import event_store, simulator_control
from trackrelay.main import app as trackrelay_app
from trackrelay.main import get_downstream_delivery_queue
from trackrelay.models import (
    DeliveryAttempt,
    DeliveryOutboxEntry,
    Event,
    Partner,
    Shipment,
)
from trackrelay.services import (
    DeliveryResult,
    DownstreamDeliveryJob,
    DownstreamDeliveryQueueError,
    RecordingDownstreamDeliveryMessage,
    deliver_and_record_normalized_event,
    process_downstream_delivery_message,
)

PARTNER_ID = "alpha-indonesia"
PARTNER_EVENT_ID = "E2E-ALPHA-PICKUP-001"
TRACKING_NUMBER = "E2E-TRK-PICKUP-001"
OUT_OF_ORDER_TRACKING_NUMBER = "E2E-TRK-OUT-OF-ORDER-001"
DELIVERED_EVENT_ID = "E2E-ALPHA-DELIVERED-001"
STALE_EVENT_ID = "E2E-ALPHA-STALE-OFD-001"
SERVER_ERROR_EVENT_ID = "E2E-ALPHA-SERVER-ERROR-001"
SERVER_ERROR_TRACKING_NUMBER = "E2E-TRK-SERVER-ERROR-001"
TIMEOUT_EVENT_ID = "E2E-ALPHA-TIMEOUT-001"
TIMEOUT_TRACKING_NUMBER = "E2E-TRK-TIMEOUT-001"
OUTAGE_CASES = (
    ("E2E-ALPHA-OUTAGE-001", "E2E-TRK-OUTAGE-001"),
    ("E2E-ALPHA-OUTAGE-002", "E2E-TRK-OUTAGE-002"),
    ("E2E-ALPHA-OUTAGE-003", "E2E-TRK-OUTAGE-003"),
)
SCENARIO_TRACKING_NUMBERS = (
    TRACKING_NUMBER,
    OUT_OF_ORDER_TRACKING_NUMBER,
    SERVER_ERROR_TRACKING_NUMBER,
    TIMEOUT_TRACKING_NUMBER,
    *(tracking_number for _, tracking_number in OUTAGE_CASES),
)


class InlineDeliveryQueue:
    """Exercise API acceptance and worker delivery in one test process."""

    def __init__(
        self,
        deliver: Callable[[NormalizedEvent, UUID], DeliveryResult],
    ) -> None:
        self._deliver = deliver

    def enqueue(self, job: DownstreamDeliveryJob) -> None:
        message = RecordingDownstreamDeliveryMessage(job)
        try:
            process_downstream_delivery_message(
                message,
                deliver_and_record_event=self._deliver,
            )
        except httpx.HTTPError as error:
            raise DownstreamDeliveryQueueError(
                "inline test delivery did not complete"
            ) from error
        assert message.acknowledged is True


def cleanup_event_and_shipment() -> None:
    """Remove only the records owned by this scenario."""
    with session_factory.begin() as session:
        scenario_event_ids = select(Event.id).where(
            Event.partner_id == PARTNER_ID,
            Event.tracking_number.in_(SCENARIO_TRACKING_NUMBERS),
        )
        session.execute(
            delete(DeliveryAttempt).where(
                DeliveryAttempt.event_id.in_(scenario_event_ids)
            )
        )
        session.execute(
            delete(Event).where(
                Event.partner_id == PARTNER_ID,
                Event.tracking_number.in_(SCENARIO_TRACKING_NUMBERS),
            )
        )
        session.execute(
            delete(Shipment).where(
                Shipment.tracking_number.in_(SCENARIO_TRACKING_NUMBERS)
            )
        )


@fixture
def configured_alpha_partner() -> Iterator[None]:
    """Provide Alpha configuration and restore any pre-existing row afterward."""
    assert engine.dialect.name == "postgresql"
    cleanup_event_and_shipment()
    event_store.clear()
    simulator_control.reset()

    with session_factory.begin() as session:
        partner = session.get(Partner, PARTNER_ID)
        partner_was_created = partner is None
        original_values: tuple[str, str, bool] | None = None

        if partner is None:
            session.add(
                Partner(
                    id=PARTNER_ID,
                    name="Courier Alpha",
                    adapter_type="courier-alpha",
                    is_active=True,
                )
            )
        else:
            original_values = (partner.name, partner.adapter_type, partner.is_active)
            partner.name = "Courier Alpha"
            partner.adapter_type = "courier-alpha"
            partner.is_active = True

    try:
        yield
    finally:
        trackrelay_app.dependency_overrides.clear()
        cleanup_event_and_shipment()
        event_store.clear()
        simulator_control.reset()

        with session_factory.begin() as session:
            partner = session.get(Partner, PARTNER_ID)
            if partner_was_created:
                if partner is not None:
                    session.delete(partner)
            elif partner is not None and original_values is not None:
                partner.name, partner.adapter_type, partner.is_active = original_values


@mark.integration
def test_alpha_pickup_travels_through_the_complete_vertical_slice(
    configured_alpha_partner: None,
) -> None:
    with TestClient(
        downstream_app,
        base_url="http://downstream.test",
    ) as downstream_client:

        def deliver_to_simulator(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_downstream_delivery_queue] = (
            lambda: InlineDeliveryQueue(deliver_to_simulator)
        )

        with TestClient(trackrelay_app) as trackrelay_client:
            response = trackrelay_client.post(
                f"/api/v1/partners/{PARTNER_ID}/events",
                json={
                    "event_id": PARTNER_EVENT_ID,
                    "tracking_number": TRACKING_NUMBER,
                    "status": "PICKUP",
                    "event_time": "2026-08-11T10:00:00+07:00",
                },
            )

    assert response.status_code == 201
    assert response.json()["processing_status"] == "processed"
    assert response.json()["duplicate"] is False
    assert response.json()["delivery_status"] == "queued"
    assert response.json()["downstream_status_code"] is None

    with session_factory() as session:
        persisted_event = session.scalar(
            select(Event).where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == PARTNER_EVENT_ID,
            )
        )
        shipment = session.get(Shipment, TRACKING_NUMBER)

        assert persisted_event is not None
        assert persisted_event.status is ShipmentStatus.PICKED_UP
        assert persisted_event.processing_status is EventProcessingStatus.PROCESSED
        assert persisted_event.state_applied is True
        attempt = session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.event_id == persisted_event.id
            )
        )
        assert attempt is not None
        assert attempt.attempt_number == 1
        assert attempt.result.value == "delivered"
        assert attempt.response_code == 202
        assert attempt.latency_ms >= 0
        assert attempt.error is None
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.PICKED_UP

    downstream_events = event_store.all()
    assert len(downstream_events) == 1
    assert downstream_events[0].partner_id == PARTNER_ID
    assert downstream_events[0].partner_event_id == PARTNER_EVENT_ID
    assert downstream_events[0].tracking_number == TRACKING_NUMBER
    assert downstream_events[0].status is ShipmentStatus.PICKED_UP


@mark.integration
def test_ten_retries_have_one_logical_event_and_one_downstream_effect(
    configured_alpha_partner: None,
) -> None:
    payload = {
        "event_id": PARTNER_EVENT_ID,
        "tracking_number": TRACKING_NUMBER,
        "status": "PICKUP",
        "event_time": "2026-08-11T10:00:00+07:00",
    }

    with TestClient(
        downstream_app,
        base_url="http://downstream.test",
    ) as downstream_client:

        def deliver_to_simulator(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_downstream_delivery_queue] = (
            lambda: InlineDeliveryQueue(deliver_to_simulator)
        )

        with TestClient(trackrelay_app) as trackrelay_client:
            responses = [
                trackrelay_client.post(
                    f"/api/v1/partners/{PARTNER_ID}/events",
                    json=payload,
                )
                for _ in range(10)
            ]

    original_event_id = responses[0].json()["event_id"]
    assert len(responses) == 10
    assert [response.status_code for response in responses] == [201] + [200] * 9
    assert [response.json()["duplicate"] for response in responses] == [False] + [
        True
    ] * 9
    assert {response.json()["event_id"] for response in responses} == {
        original_event_id
    }
    assert all(
        response.json()["delivery_status"] == "skipped_duplicate"
        and response.json()["downstream_status_code"] is None
        for response in responses[1:]
    )

    with session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == PARTNER_EVENT_ID,
            )
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(DeliveryAttempt.event_id == UUID(original_event_id))
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == PARTNER_EVENT_ID,
                Event.state_applied.is_(True),
            )
        ) == 1
        shipment = session.get(Shipment, TRACKING_NUMBER)
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.PICKED_UP

    downstream_events = event_store.all()
    assert len(downstream_events) == 1
    assert downstream_events[0].partner_event_id == PARTNER_EVENT_ID


@mark.integration
@mark.parametrize(
    (
        "partner_event_id",
        "tracking_number",
        "downstream_response_code",
        "expected_result",
    ),
    [
        (
            SERVER_ERROR_EVENT_ID,
            SERVER_ERROR_TRACKING_NUMBER,
            500,
            DeliveryAttemptResult.HTTP_ERROR,
        ),
        (
            TIMEOUT_EVENT_ID,
            TIMEOUT_TRACKING_NUMBER,
            None,
            DeliveryAttemptResult.TRANSPORT_ERROR,
        ),
    ],
)
def test_delivery_failure_keeps_event_shipment_and_attempt_committed(
    configured_alpha_partner: None,
    partner_event_id: str,
    tracking_number: str,
    downstream_response_code: int | None,
    expected_result: DeliveryAttemptResult,
) -> None:
    def downstream_response(request: httpx.Request) -> httpx.Response:
        if downstream_response_code is None:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(downstream_response_code)

    with httpx.Client(transport=httpx.MockTransport(downstream_response)) as client:

        def deliver_to_downstream(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=client,
            )

        trackrelay_app.dependency_overrides[get_downstream_delivery_queue] = (
            lambda: InlineDeliveryQueue(deliver_to_downstream)
        )

        with TestClient(trackrelay_app) as trackrelay_client:
            response = trackrelay_client.post(
                f"/api/v1/partners/{PARTNER_ID}/events",
                json={
                    "event_id": partner_event_id,
                    "tracking_number": tracking_number,
                    "status": "PICKUP",
                    "event_time": "2026-08-14T10:00:00+07:00",
                },
            )

    assert response.status_code == 201
    assert response.json()["delivery_status"] == "queued"

    with session_factory() as session:
        persisted_event = session.scalar(
            select(Event).where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == partner_event_id,
            )
        )
        shipment = session.get(Shipment, tracking_number)

        assert persisted_event is not None
        assert persisted_event.processing_status is EventProcessingStatus.PROCESSED
        assert persisted_event.state_applied is True
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.PICKED_UP

        attempt = session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.event_id == persisted_event.id
            )
        )
        assert attempt is not None
        assert attempt.attempt_number == 1
        assert attempt.result is expected_result
        assert attempt.response_code == downstream_response_code
        assert attempt.error is not None
        outbox_entry = session.get(DeliveryOutboxEntry, persisted_event.id)
        assert outbox_entry is not None
        assert outbox_entry.published_at is None


@mark.integration
def test_unavailable_outage_persists_events_and_duplicate_retries_stay_safe(
    configured_alpha_partner: None,
) -> None:
    payloads = [
        {
            "event_id": partner_event_id,
            "tracking_number": tracking_number,
            "status": "PICKUP",
            "event_time": "2026-08-14T11:00:00+07:00",
        }
        for partner_event_id, tracking_number in OUTAGE_CASES
    ]

    with TestClient(
        downstream_app,
        base_url="http://downstream.test",
    ) as downstream_client:
        mode_response = downstream_client.put(
            "/control/mode",
            json={"mode": "UNAVAILABLE"},
        )
        assert mode_response.status_code == 200

        def deliver_to_simulator(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_downstream_delivery_queue] = (
            lambda: InlineDeliveryQueue(deliver_to_simulator)
        )

        with TestClient(trackrelay_app) as trackrelay_client:
            failed_responses = [
                trackrelay_client.post(
                    f"/api/v1/partners/{PARTNER_ID}/events",
                    json=payload,
                )
                for payload in payloads
            ]
            retry_responses = [
                trackrelay_client.post(
                    f"/api/v1/partners/{PARTNER_ID}/events",
                    json=payload,
                )
                for payload in payloads
            ]

    assert [response.status_code for response in failed_responses] == [201] * 3
    assert all(
        response.json()["delivery_status"] == "queued"
        for response in failed_responses
    )
    assert [response.status_code for response in retry_responses] == [200] * 3
    assert all(
        response.json()["duplicate"] is True
        and response.json()["delivery_status"] == "skipped_duplicate"
        and response.json()["downstream_status_code"] is None
        for response in retry_responses
    )

    with session_factory() as session:
        events = tuple(
            session.scalars(
                select(Event)
                .where(
                    Event.partner_id == PARTNER_ID,
                    Event.partner_event_id.in_(
                        [partner_event_id for partner_event_id, _ in OUTAGE_CASES]
                    ),
                )
                .order_by(Event.partner_event_id)
            )
        )
        shipments = tuple(
            session.scalars(
                select(Shipment).where(
                    Shipment.tracking_number.in_(
                        [tracking_number for _, tracking_number in OUTAGE_CASES]
                    )
                )
            )
        )
        attempts = tuple(
            session.scalars(
                select(DeliveryAttempt)
                .where(
                    DeliveryAttempt.event_id.in_([event.id for event in events])
                )
                .order_by(DeliveryAttempt.event_id)
            )
        )

        assert [event.partner_event_id for event in events] == [
            partner_event_id for partner_event_id, _ in OUTAGE_CASES
        ]
        assert all(
            event.processing_status is EventProcessingStatus.PROCESSED
            and event.state_applied is True
            for event in events
        )
        assert {response.json()["event_id"] for response in retry_responses} == {
            str(event.id) for event in events
        }
        assert len(shipments) == 3
        assert all(
            shipment.current_status is ShipmentStatus.PICKED_UP
            for shipment in shipments
        )
        assert len(attempts) == 3
        assert all(
            attempt.attempt_number == 1
            and attempt.result is DeliveryAttemptResult.HTTP_ERROR
            and attempt.response_code == 503
            and attempt.error is not None
            for attempt in attempts
        )
        assert all(
            session.get(DeliveryOutboxEntry, event.id).published_at is None
            for event in events
        )

    assert event_store.all() == ()


@mark.integration
def test_out_of_order_event_is_audited_without_reversing_delivery(
    configured_alpha_partner: None,
) -> None:
    with TestClient(
        downstream_app,
        base_url="http://downstream.test",
    ) as downstream_client:

        def deliver_to_simulator(
            event: NormalizedEvent,
            event_id: UUID,
        ) -> DeliveryResult:
            return deliver_and_record_normalized_event(
                event,
                event_id=event_id,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_downstream_delivery_queue] = (
            lambda: InlineDeliveryQueue(deliver_to_simulator)
        )

        with TestClient(trackrelay_app) as trackrelay_client:
            delivered_response = trackrelay_client.post(
                f"/api/v1/partners/{PARTNER_ID}/events",
                json={
                    "event_id": DELIVERED_EVENT_ID,
                    "tracking_number": OUT_OF_ORDER_TRACKING_NUMBER,
                    "status": "POD",
                    "event_time": "2026-08-13T10:03:00+07:00",
                },
            )
            stale_response = trackrelay_client.post(
                f"/api/v1/partners/{PARTNER_ID}/events",
                json={
                    "event_id": STALE_EVENT_ID,
                    "tracking_number": OUT_OF_ORDER_TRACKING_NUMBER,
                    "status": "OFD",
                    "event_time": "2026-08-13T10:01:00+07:00",
                },
            )
            history_response = trackrelay_client.get(
                f"/api/v1/shipments/{OUT_OF_ORDER_TRACKING_NUMBER}/events"
            )

    assert delivered_response.status_code == 201
    assert stale_response.status_code == 201
    assert history_response.status_code == 200

    history = history_response.json()
    assert [item["partner_event_id"] for item in history] == [
        STALE_EVENT_ID,
        DELIVERED_EVENT_ID,
    ]
    assert [item["status"] for item in history] == [
        "out_for_delivery",
        "delivered",
    ]
    assert [item["state_applied"] for item in history] == [False, True]
    assert history[0]["state_rejection_reason"] == "stale_event"
    assert history[1]["state_rejection_reason"] is None

    with session_factory() as session:
        shipment = session.get(Shipment, OUT_OF_ORDER_TRACKING_NUMBER)
        events = tuple(
            session.scalars(
                select(Event)
                .where(Event.tracking_number == OUT_OF_ORDER_TRACKING_NUMBER)
                .order_by(Event.occurred_at.asc())
            )
        )

        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.DELIVERED
        assert shipment.current_status_occurred_at == datetime(
            2026,
            8,
            13,
            3,
            3,
            tzinfo=UTC,
        )
        assert len(events) == 2
        assert [event.state_applied for event in events] == [False, True]
        assert session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(DeliveryAttempt.event_id.in_([event.id for event in events]))
        ) == 2

    downstream_events = event_store.all()
    assert [event.partner_event_id for event in downstream_events] == [
        DELIVERED_EVENT_ID,
        STALE_EVENT_ID,
    ]
