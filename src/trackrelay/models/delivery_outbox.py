"""Durable downstream-delivery outbox persistence model."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base


class DeliveryOutboxEntry(Base):
    """One event whose SQS delivery job must be published at least once."""

    __tablename__ = "delivery_outbox"
    __table_args__ = (
        CheckConstraint(
            "published_at IS NULL OR published_at >= created_at",
            name="ck_delivery_outbox_publication_time",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "events.id",
            name="fk_delivery_outbox_event_id_events",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
