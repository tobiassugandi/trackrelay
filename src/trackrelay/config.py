"""Typed application configuration loaded from the environment."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    downstream_url: str = "http://127.0.0.1:8001"
    downstream_timeout_seconds: float = 5.0
