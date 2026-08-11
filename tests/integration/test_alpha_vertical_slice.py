"""End-to-end test for the first complete Courier Alpha event path."""

from collections.abc import Iterator

from fastapi.testclient import TestClient
from pytest import fixture, mark
from sqlalchemy import delete, select

from trackrelay.database import engine, session_factory
from trackrelay.domain import EventProcessingStatus, NormalizedEvent, ShipmentStatus
from trackrelay.downstream.main import app as downstream_app
from trackrelay.downstream.main import event_store
from trackrelay.main import app as trackrelay_app
from trackrelay.main import get_event_deliverer
from trackrelay.models import Event, Partner, Shipment
from trackrelay.services import DeliveryResult, deliver_normalized_event

PARTNER_ID = "courier-alpha"
PARTNER_EVENT_ID = "E2E-ALPHA-PICKUP-001"
TRACKING_NUMBER = "E2E-TRK-PICKUP-001"


def cleanup_event_and_shipment() -> None:
    """Remove only the records owned by this scenario."""
    with session_factory.begin() as session:
        session.execute(
            delete(Event).where(
                Event.partner_id == PARTNER_ID,
                Event.partner_event_id == PARTNER_EVENT_ID,
            )
        )
        session.execute(
            delete(Shipment).where(Shipment.tracking_number == TRACKING_NUMBER)
        )


@fixture
def configured_alpha_partner() -> Iterator[None]:
    """Provide Alpha configuration and restore any pre-existing row afterward."""
    assert engine.dialect.name == "postgresql"
    cleanup_event_and_shipment()
    event_store.clear()

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

        def deliver_to_simulator(event: NormalizedEvent) -> DeliveryResult:
            return deliver_normalized_event(
                event,
                downstream_url="http://downstream.test",
                client=downstream_client,
            )

        trackrelay_app.dependency_overrides[get_event_deliverer] = (
            lambda: deliver_to_simulator
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
    assert response.json()["delivery_status"] == "delivered"
    assert response.json()["downstream_status_code"] == 202

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
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.PICKED_UP

    downstream_events = event_store.all()
    assert len(downstream_events) == 1
    assert downstream_events[0].partner_event_id == PARTNER_EVENT_ID
    assert downstream_events[0].tracking_number == TRACKING_NUMBER
    assert downstream_events[0].status is ShipmentStatus.PICKED_UP
