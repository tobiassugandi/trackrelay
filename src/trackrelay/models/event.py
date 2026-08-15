"""Shipment event persistence model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base
from trackrelay.domain import EventProcessingStatus, ShipmentStatus

EVENT_STATUS_TYPE = Enum(
    ShipmentStatus,
    name="event_shipment_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_type: [status.value for status in enum_type],
    length=32,
)

EVENT_PROCESSING_STATUS_TYPE = Enum(
    EventProcessingStatus,
    name="event_processing_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_type: [status.value for status in enum_type],
    length=32,
)


class Event(Base):
    """An immutable normalized event and its processing outcome."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('created', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered')",
            name="ck_events_status",
        ),
        CheckConstraint(
            "processing_status IN ('received', 'processed', 'failed')",
            name="ck_events_processing_status",
        ),
        UniqueConstraint(
            "partner_id",
            "partner_event_id",
            name="uq_events_partner_event",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    partner_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("partners.id", name="fk_events_partner_id_partners"),
        nullable=False,
    )
    partner_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tracking_number: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "shipments.tracking_number",
            name="fk_events_tracking_number_shipments",
        ),
        nullable=False,
    )
    status: Mapped[ShipmentStatus] = mapped_column(EVENT_STATUS_TYPE, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    test_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("test_runs.id", name="fk_events_test_run_id_test_runs"),
        nullable=True,
        index=True,
    )
    processing_status: Mapped[EventProcessingStatus] = mapped_column(
        EVENT_PROCESSING_STATUS_TYPE,
        nullable=False,
        default=EventProcessingStatus.RECEIVED,
        server_default=EventProcessingStatus.RECEIVED.value,
    )
    state_applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    state_rejection_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
