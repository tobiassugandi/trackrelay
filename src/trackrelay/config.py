"""Typed application configuration loaded from the environment."""

from typing import Annotated, Literal, Self
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PositiveInteger = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
SqsWaitTimeSeconds = Annotated[int, Field(ge=0, le=20)]
SqsVisibilityTimeoutSeconds = Annotated[int, Field(gt=0, le=43_200)]
SqsMaxMessages = Annotated[int, Field(gt=0, le=10)]
DatabasePort = Annotated[int, Field(gt=0, le=65_535)]
DatabaseHost = Annotated[
    str,
    Field(min_length=1, pattern=r"^[A-Za-z0-9.-]+$"),
]


class Settings(BaseSettings):
    """TrackRelay runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TRACKRELAY_",
        extra="ignore",
    )

    app_name: str = "TrackRelay"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    database_url: str = (
        "postgresql+psycopg://trackrelay:trackrelay@localhost:5433/trackrelay"
    )
    database_host: DatabaseHost | None = None
    database_port: DatabasePort = 5432
    database_name: str = "trackrelay"
    database_user: str = "trackrelay"
    database_password: SecretStr | None = None
    database_sslmode: Literal["disable", "require", "verify-ca", "verify-full"] | None = (
        None
    )
    database_pool_size: PositiveInteger = 5
    database_max_overflow: NonNegativeInteger = 10
    downstream_url: str = "http://127.0.0.1:8001"
    downstream_timeout_seconds: PositiveFloat = 5.0
    delivery_queue_backend: Literal["recording", "sqs"] = "recording"
    sqs_queue_url: str | None = None
    sqs_dead_letter_queue_arn: str | None = None
    sqs_max_receive_count: PositiveInteger = 5
    sqs_wait_time_seconds: SqsWaitTimeSeconds = 20
    sqs_visibility_timeout_seconds: SqsVisibilityTimeoutSeconds = 120
    sqs_max_messages: SqsMaxMessages = 10
    aws_region: str = "ap-southeast-3"
    scaling_metrics_queue_name: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]+-delivery$"
    )

    def database_connection_url(self) -> str:
        """Build a database URL without storing the injected password in the model."""
        if self.database_host is None or self.database_password is None:
            return self.database_url
        username = quote(self.database_user, safe="")
        password = quote(self.database_password.get_secret_value(), safe="")
        database = quote(self.database_name, safe="")
        query = f"?sslmode={self.database_sslmode}" if self.database_sslmode else ""
        return (
            f"postgresql+psycopg://{username}:{password}@{self.database_host}:"
            f"{self.database_port}/{database}{query}"
        )

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        """Reject incomplete database and internally unsafe SQS configuration."""
        if (self.database_host is None) != (self.database_password is None):
            raise ValueError(
                "database_host and database_password must be configured together"
            )
        if self.delivery_queue_backend == "sqs":
            if not self.sqs_queue_url:
                raise ValueError(
                    "sqs_queue_url is required for the SQS queue backend"
                )
            if not self.sqs_dead_letter_queue_arn:
                raise ValueError(
                    "sqs_dead_letter_queue_arn is required for the SQS queue backend"
                )
            minimum_visibility = (
                self.downstream_timeout_seconds * self.sqs_max_messages
            )
            if self.sqs_visibility_timeout_seconds <= minimum_visibility:
                raise ValueError(
                    "sqs_visibility_timeout_seconds must exceed downstream "
                    "timeout multiplied by sqs_max_messages"
                )
        return self
