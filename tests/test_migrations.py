"""Tests for the Alembic runtime configuration boundary."""

from contextlib import nullcontext
from pathlib import Path
from runpy import run_path

import alembic.context
import sqlalchemy
from pytest import MonkeyPatch


def test_online_migration_assembles_the_injected_database_url(
    monkeypatch: MonkeyPatch,
) -> None:
    """Cloud migrations must not silently use the localhost URL fallback."""
    monkeypatch.setenv(
        "TRACKRELAY_DATABASE_URL",
        "postgresql+psycopg://wrong:wrong@localhost:5433/wrong",
    )
    monkeypatch.setenv("TRACKRELAY_DATABASE_HOST", "database.example.internal")
    monkeypatch.setenv("TRACKRELAY_DATABASE_PORT", "5432")
    monkeypatch.setenv("TRACKRELAY_DATABASE_NAME", "track/relay")
    monkeypatch.setenv("TRACKRELAY_DATABASE_USER", "trackrelay_admin")
    monkeypatch.setenv(
        "TRACKRELAY_DATABASE_PASSWORD",
        "secret:@/?# value",
    )
    monkeypatch.setenv("TRACKRELAY_DATABASE_SSLMODE", "require")

    observed: dict[str, object] = {}

    class FakeEngine:
        def connect(self):
            return nullcontext(object())

    def fake_create_engine(url: str, **arguments: object) -> FakeEngine:
        observed["url"] = url
        observed["arguments"] = arguments
        return FakeEngine()

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setattr(alembic.context, "config", object(), raising=False)
    monkeypatch.setattr(alembic.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(alembic.context, "configure", lambda **_arguments: None)
    monkeypatch.setattr(alembic.context, "begin_transaction", nullcontext)
    monkeypatch.setattr(alembic.context, "run_migrations", lambda: None)

    run_path(Path("migrations/env.py"))

    assert observed["url"] == (
        "postgresql+psycopg://trackrelay_admin:secret%3A%40%2F%3F%23%20value@"
        "database.example.internal:5432/track%2Frelay?sslmode=require"
    )
