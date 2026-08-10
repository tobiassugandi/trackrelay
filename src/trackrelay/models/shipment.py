"""Shipment persistence model."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base
from trackrelay.domain import ShipmentStatus

SHIPMENT_STATUS_TYPE = Enum(
    ShipmentStatus,
    name="shipment_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_type: [status.value for status in enum_type],
    length=32,
)


class Shipment(Base):
    """The latest accepted state for one tracked shipment."""

    __tablename__ = "shipments"
    __table_args__ = (
        CheckConstraint(
            "current_status IN "
            "('created', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered')",
            name="ck_shipments_current_status",
        ),
    )

    tracking_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_status: Mapped[ShipmentStatus] = mapped_column(
        SHIPMENT_STATUS_TYPE,
        nullable=False,
    )
    current_status_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
