"""Create the delivery attempts table.

Revision ID: 0005_delivery_attempts
Revises: 0004_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_delivery_attempts"
down_revision: str | Sequence[str] | None = "0004_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the delivery attempts table."""
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "result",
            sa.Enum(
                "delivered",
                "http_error",
                "transport_error",
                name="delivery_attempt_result",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_delivery_attempts_number",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="ck_delivery_attempts_latency"),
        sa.CheckConstraint(
            "result IN ('delivered', 'http_error', 'transport_error')",
            name="ck_delivery_attempts_result",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_delivery_attempts_event_id_events",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "attempt_number",
            name="uq_delivery_attempts_event_number",
        ),
    )


def downgrade() -> None:
    """Drop the delivery attempts table."""
    op.drop_table("delivery_attempts")
