"""Typed application configuration loaded from the environment."""

from typing import Annotated, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]


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
    downstream_timeout_seconds: float = 5.0
