"""Downstream delivery-attempt persistence model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base
from trackrelay.domain import DeliveryAttemptResult

DELIVERY_ATTEMPT_RESULT_TYPE = Enum(
    DeliveryAttemptResult,
    name="delivery_attempt_result",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda enum_type: [result.value for result in enum_type],
    length=32,
)


class DeliveryAttempt(Base):
    """One observable attempt to deliver a logical event downstream."""

    __tablename__ = "delivery_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="ck_delivery_attempts_number"),
        CheckConstraint("latency_ms >= 0", name="ck_delivery_attempts_latency"),
        CheckConstraint(
            "result IN ('delivered', 'http_error', 'transport_error')",
            name="ck_delivery_attempts_result",
        ),
        UniqueConstraint(
            "event_id",
            "attempt_number",
            name="uq_delivery_attempts_event_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("events.id", name="fk_delivery_attempts_event_id_events"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[DeliveryAttemptResult] = mapped_column(
        DELIVERY_ATTEMPT_RESULT_TYPE,
        nullable=False,
    )
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
