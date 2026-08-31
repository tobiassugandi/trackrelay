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
    monkeypatch.setenv(
        "TRACKRELAY_SQS_DEAD_LETTER_QUEUE_ARN",
        "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq",
    )
    monkeypatch.setenv("TRACKRELAY_SQS_MAX_RECEIVE_COUNT", "4")
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
    assert settings.sqs_dead_letter_queue_arn == (
        "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq"
    )
    assert settings.sqs_max_receive_count == 4
    assert settings.sqs_wait_time_seconds == 10
    assert settings.sqs_visibility_timeout_seconds == 45
    assert settings.sqs_max_messages == 7
    assert settings.aws_region == "ap-southeast-3"


def test_database_components_build_an_encoded_tls_url(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKRELAY_DATABASE_HOST", "database.example.internal")
    monkeypatch.setenv("TRACKRELAY_DATABASE_PORT", "5432")
    monkeypatch.setenv("TRACKRELAY_DATABASE_NAME", "track/relay")
    monkeypatch.setenv("TRACKRELAY_DATABASE_USER", "trackrelay_admin")
    monkeypatch.setenv("TRACKRELAY_DATABASE_PASSWORD", "secret:@/?# value")
    monkeypatch.setenv("TRACKRELAY_DATABASE_SSLMODE", "require")

    settings = Settings(_env_file=None)

    assert settings.database_connection_url() == (
        "postgresql+psycopg://trackrelay_admin:secret%3A%40%2F%3F%23%20value@"
        "database.example.internal:5432/track%2Frelay?sslmode=require"
    )
    assert "secret:@/?# value" not in repr(settings)


def test_database_components_require_host_and_password_together() -> None:
    with raises(ValueError, match="must be configured together"):
        Settings(
            _env_file=None,
            database_host="database.example.internal",
            database_password=None,
        )


def test_sqs_backend_requires_a_queue_url() -> None:
    with raises(ValueError, match="sqs_queue_url is required"):
        Settings(
            _env_file=None,
            delivery_queue_backend="sqs",
            sqs_queue_url=None,
            sqs_dead_letter_queue_arn=(
                "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq"
            ),
        )


def test_sqs_backend_requires_a_dead_letter_queue() -> None:
    with raises(ValueError, match="sqs_dead_letter_queue_arn is required"):
        Settings(
            _env_file=None,
            delivery_queue_backend="sqs",
            sqs_queue_url=(
                "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"
            ),
            sqs_dead_letter_queue_arn=None,
        )


def test_sqs_visibility_covers_the_configured_sequential_batch() -> None:
    with raises(ValueError, match="must exceed downstream timeout"):
        Settings(
            _env_file=None,
            delivery_queue_backend="sqs",
            sqs_queue_url=(
                "https://sqs.ap-southeast-3.amazonaws.com/123456789012/jobs"
            ),
            sqs_dead_letter_queue_arn=(
                "arn:aws:sqs:ap-southeast-3:123456789012:jobs-dlq"
            ),
            downstream_timeout_seconds=5,
            sqs_visibility_timeout_seconds=50,
            sqs_max_messages=10,
        )
