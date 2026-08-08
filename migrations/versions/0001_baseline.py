"""Establish an empty migration baseline.

Revision ID: 0001_baseline
Revises:
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish the baseline before domain tables exist."""


def downgrade() -> None:
    """Remove the empty baseline."""
