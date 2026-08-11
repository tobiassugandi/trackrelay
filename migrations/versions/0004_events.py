"""Create the events table.

Revision ID: 0004_events
Revises: 0003_shipments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_events"
down_revision: str | Sequence[str] | None = "0003_shipments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the events table."""
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("partner_id", sa.String(length=64), nullable=False),
        sa.Column("partner_event_id", sa.String(length=128), nullable=False),
        sa.Column("tracking_number", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "picked_up",
                "in_transit",
                "out_for_delivery",
                "delivered",
                name="event_shipment_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column(
            "processing_status",
            sa.Enum(
                "received",
                "processed",
                "failed",
                name="event_processing_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="received",
            nullable=False,
        ),
        sa.Column(
            "state_applied",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("state_rejection_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "processing_status IN ('received', 'processed', 'failed')",
            name="ck_events_processing_status",
        ),
        sa.CheckConstraint(
            "status IN "
            "('created', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered')",
            name="ck_events_status",
        ),
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["partners.id"],
            name="fk_events_partner_id_partners",
        ),
        sa.ForeignKeyConstraint(
            ["tracking_number"],
            ["shipments.tracking_number"],
            name="fk_events_tracking_number_shipments",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "partner_id",
            "partner_event_id",
            name="uq_events_partner_event",
        ),
    )


def downgrade() -> None:
    """Drop the events table."""
    op.drop_table("events")
