"""Create the shipments table.

Revision ID: 0003_shipments
Revises: 0002_partners
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_shipments"
down_revision: str | Sequence[str] | None = "0002_partners"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the shipments table."""
    op.create_table(
        "shipments",
        sa.Column("tracking_number", sa.String(length=64), nullable=False),
        sa.Column(
            "current_status",
            sa.Enum(
                "created",
                "picked_up",
                "in_transit",
                "out_for_delivery",
                "delivered",
                name="shipment_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "current_status_occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "current_status IN "
            "('created', 'picked_up', 'in_transit', 'out_for_delivery', 'delivered')",
            name="ck_shipments_current_status",
        ),
        sa.PrimaryKeyConstraint("tracking_number"),
    )


def downgrade() -> None:
    """Drop the shipments table."""
    op.drop_table("shipments")
