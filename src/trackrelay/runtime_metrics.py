"""Read-only process and database-pool measurements for local experiments."""

import resource
import sys
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from trackrelay.database import engine as default_engine

NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]


class DatabasePoolMetrics(BaseModel):
    """A point-in-time view of SQLAlchemy's connection pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_out: NonNegativeInteger | None
    checked_in: NonNegativeInteger | None
    pool_size: NonNegativeInteger | None
    overflow: int | None


class RuntimeMetricsSnapshot(BaseModel):
    """One API-process resource sample captured during an experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    captured_at: datetime
    process_cpu_seconds: NonNegativeFloat
    process_max_rss_bytes: NonNegativeInteger
    database_pool: DatabasePoolMetrics


def _pool_value(database_engine: Engine, method_name: str) -> int | None:
    method = getattr(database_engine.pool, method_name, None)
    return int(method()) if callable(method) else None


def _maximum_resident_memory_bytes(maximum_resident_memory: int) -> int:
    # macOS reports bytes; Linux reports kibibytes.
    return (
        maximum_resident_memory
        if sys.platform == "darwin"
        else maximum_resident_memory * 1024
    )


def capture_runtime_metrics(
    database_engine: Engine = default_engine,
) -> RuntimeMetricsSnapshot:
    """Capture API-process and SQLAlchemy-pool measurements without I/O."""
    process_usage = resource.getrusage(resource.RUSAGE_SELF)
    return RuntimeMetricsSnapshot(
        captured_at=datetime.now(UTC),
        process_cpu_seconds=process_usage.ru_utime + process_usage.ru_stime,
        process_max_rss_bytes=_maximum_resident_memory_bytes(
            process_usage.ru_maxrss
        ),
        database_pool=DatabasePoolMetrics(
            checked_out=_pool_value(database_engine, "checkedout"),
            checked_in=_pool_value(database_engine, "checkedin"),
            pool_size=_pool_value(database_engine, "size"),
            overflow=_pool_value(database_engine, "overflow"),
        ),
    )
