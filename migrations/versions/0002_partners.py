"""Create the partners table.

Revision ID: 0002_partners
Revises: 0001_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_partners"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the partners table."""
    op.create_table(
        "partners",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop the partners table."""
    op.drop_table("partners")
