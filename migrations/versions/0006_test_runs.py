"""Create test runs and attach synthetic events.

Revision ID: 0006_test_runs
Revises: 0005_delivery_attempts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_test_runs"
down_revision: str | Sequence[str] | None = "0005_delivery_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create experiment runs and their optional event association."""
    op.create_table(
        "test_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scenario_name", sa.String(length=120), nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("expected_event_count", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_test_runs_completion",
        ),
        sa.CheckConstraint(
            "expected_event_count >= 0",
            name="ck_test_runs_expected_event_count",
        ),
        sa.CheckConstraint(
            "length(trim(scenario_name)) > 0",
            name="ck_test_runs_scenario_name",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("events", sa.Column("test_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_events_test_run_id_test_runs",
        "events",
        "test_runs",
        ["test_run_id"],
        ["id"],
    )
    op.create_index("ix_events_test_run_id", "events", ["test_run_id"])


def downgrade() -> None:
    """Remove the event association and experiment runs."""
    op.drop_index("ix_events_test_run_id", table_name="events")
    op.drop_constraint(
        "fk_events_test_run_id_test_runs",
        "events",
        type_="foreignkey",
    )
    op.drop_column("events", "test_run_id")
    op.drop_table("test_runs")
