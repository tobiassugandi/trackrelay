"""Create the durable delivery outbox.

Revision ID: 0007_delivery_outbox
Revises: 0006_test_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_delivery_outbox"
down_revision: str | Sequence[str] | None = "0006_test_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one durable publication record per accepted event."""
    op.create_table(
        "delivery_outbox",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at >= created_at",
            name="ck_delivery_outbox_publication_time",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_delivery_outbox_event_id_events",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_delivery_outbox_published_at",
        "delivery_outbox",
        ["published_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the durable delivery outbox."""
    op.drop_index(
        "ix_delivery_outbox_published_at",
        table_name="delivery_outbox",
    )
    op.drop_table("delivery_outbox")
