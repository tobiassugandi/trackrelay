"""Alembic migration environment."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

import trackrelay.models  # noqa: F401 -- Register models with Base.metadata.
from trackrelay.config import Settings
from trackrelay.database import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    """Resolve either a complete local URL or the assembled cloud URL."""
    return Settings().database_connection_url()


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using the configured database connection."""
    connectable = create_engine(database_url(), poolclass=NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
