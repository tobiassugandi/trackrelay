"""Courier partner persistence model."""

from sqlalchemy import Boolean, String, true
from sqlalchemy.orm import Mapped, mapped_column

from trackrelay.database import Base


class Partner(Base):
    """A business-partner identity and its reusable payload-adapter selection."""

    __tablename__ = "partners"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
