"""Tests for the shipment persistence model."""

from datetime import UTC, datetime

from trackrelay.database import create_database_engine, create_session_factory
from trackrelay.domain import ShipmentStatus
from trackrelay.models import Shipment


def test_shipment_model_round_trip() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Shipment.__table__.create(engine)
    session_factory = create_session_factory(engine)
    occurred_at = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)

    with session_factory() as session:
        session.add(
            Shipment(
                tracking_number="TRK-001",
                current_status=ShipmentStatus.CREATED,
                current_status_occurred_at=occurred_at,
            )
        )
        session.commit()

    with session_factory() as session:
        shipment = session.get(Shipment, "TRK-001")
        assert shipment is not None
        assert shipment.current_status is ShipmentStatus.CREATED
        assert shipment.current_status_occurred_at.replace(tzinfo=UTC) == occurred_at
        assert shipment.created_at is not None
        assert shipment.updated_at is not None

    engine.dispose()
