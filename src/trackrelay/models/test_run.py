"""Repeatable experiment-run persistence model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base


class TestRun(Base):
    """The definition and lifecycle of one synthetic experiment run."""

    __test__ = False
    __tablename__ = "test_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(scenario_name)) > 0",
            name="ck_test_runs_scenario_name",
        ),
        CheckConstraint(
            "expected_event_count >= 0",
            name="ck_test_runs_expected_event_count",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_test_runs_completion",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scenario_name: Mapped[str] = mapped_column(String(120), nullable=False)
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    expected_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
