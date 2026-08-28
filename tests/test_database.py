"""Tests for database infrastructure."""

from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from trackrelay.database import (
    check_database_connection,
    create_database_engine,
    create_session_factory,
)


def test_engine_and_session_factory_execute_queries() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    session_factory = create_session_factory(engine)

    assert check_database_connection(engine) is True

    with session_factory() as session:
        assert session.scalar(text("SELECT 1")) == 1

    engine.dispose()


def test_postgresql_pool_capacity_is_explicit() -> None:
    engine = create_database_engine(
        "postgresql+psycopg://trackrelay:secret@database/trackrelay",
        pool_size=7,
        max_overflow=3,
    )

    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == 7
    assert engine.pool._max_overflow == 3

    engine.dispose()
