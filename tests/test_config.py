"""Tests for application configuration."""

from pytest import MonkeyPatch

from trackrelay.config import Settings


def test_settings_load_prefixed_environment_variables(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKRELAY_APP_NAME", "TrackRelay Test")
    monkeypatch.setenv("TRACKRELAY_ENVIRONMENT", "test")
    monkeypatch.setenv("TRACKRELAY_DEBUG", "true")
    monkeypatch.setenv("TRACKRELAY_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    settings = Settings(_env_file=None)

    assert settings.app_name == "TrackRelay Test"
    assert settings.environment == "test"
    assert settings.debug is True
    assert settings.database_url == "sqlite+pysqlite:///:memory:"
