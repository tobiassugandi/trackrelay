"""Tests for application configuration."""

from pytest import MonkeyPatch, raises

from trackrelay.config import Settings


def test_settings_load_prefixed_environment_variables(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKRELAY_APP_NAME", "TrackRelay Test")
    monkeypatch.setenv("TRACKRELAY_ENVIRONMENT", "test")
    monkeypatch.setenv("TRACKRELAY_DEBUG", "true")
    monkeypatch.setenv("TRACKRELAY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("TRACKRELAY_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("TRACKRELAY_DATABASE_MAX_OVERFLOW", "3")
    monkeypatch.setenv("TRACKRELAY_DOWNSTREAM_URL", "http://downstream.test")
    monkeypatch.setenv("TRACKRELAY_DOWNSTREAM_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("TRACKRELAY_DELIVERY_QUEUE_BACKEND", "sqs")
    monkeypatch.setenv(
        "TRACKRELAY_SQS_QUEUE_URL",
        "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs",
    )
    monkeypatch.setenv("TRACKRELAY_SQS_WAIT_TIME_SECONDS", "10")
    monkeypatch.setenv("TRACKRELAY_SQS_VISIBILITY_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("TRACKRELAY_SQS_MAX_MESSAGES", "7")
    monkeypatch.setenv("TRACKRELAY_AWS_REGION", "ap-southeast-3")

    settings = Settings(_env_file=None)

    assert settings.app_name == "TrackRelay Test"
    assert settings.environment == "test"
    assert settings.debug is True
    assert settings.database_url == "sqlite+pysqlite:///:memory:"
    assert settings.database_pool_size == 7
    assert settings.database_max_overflow == 3
    assert settings.downstream_url == "http://downstream.test"
    assert settings.downstream_timeout_seconds == 2.5
    assert settings.delivery_queue_backend == "sqs"
    assert settings.sqs_queue_url == (
        "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"
    )
    assert settings.sqs_wait_time_seconds == 10
    assert settings.sqs_visibility_timeout_seconds == 45
    assert settings.sqs_max_messages == 7
    assert settings.aws_region == "ap-southeast-3"


def test_sqs_backend_requires_a_queue_url() -> None:
    with raises(ValueError, match="sqs_queue_url is required"):
        Settings(
            _env_file=None,
            delivery_queue_backend="sqs",
            sqs_queue_url=None,
        )


def test_sqs_visibility_covers_the_configured_sequential_batch() -> None:
    with raises(ValueError, match="must exceed downstream timeout"):
        Settings(
            _env_file=None,
            delivery_queue_backend="sqs",
            sqs_queue_url=(
                "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"
            ),
            downstream_timeout_seconds=5,
            sqs_visibility_timeout_seconds=50,
            sqs_max_messages=10,
        )
