"""Typed application configuration loaded from the environment."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PositiveInteger = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
SqsWaitTimeSeconds = Annotated[int, Field(ge=0, le=20)]
SqsVisibilityTimeoutSeconds = Annotated[int, Field(gt=0, le=43_200)]
SqsMaxMessages = Annotated[int, Field(gt=0, le=10)]


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

    @model_validator(mode="after")
    def validate_sqs_runtime(self) -> Self:
        """Reject incomplete or internally unsafe SQS configuration."""
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
