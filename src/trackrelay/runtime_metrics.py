"""Read-only process, host, and database-pool experiment measurements."""

import os
import resource
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import active_count
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from trackrelay.database import engine as default_engine

NonNegativeFloat = Annotated[float, Field(ge=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
PositiveInteger = Annotated[int, Field(gt=0)]


class LogicalCpuTimes(BaseModel):
    """Cumulative Linux scheduler time for one logical CPU."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_index: NonNegativeInteger
    total_seconds: NonNegativeFloat
    idle_seconds: NonNegativeFloat


class DatabasePoolMetrics(BaseModel):
    """A point-in-time view of SQLAlchemy's connection pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_out: NonNegativeInteger | None
    checked_in: NonNegativeInteger | None
    pool_size: NonNegativeInteger | None
    overflow: int | None
    max_overflow: NonNegativeInteger | None


class RuntimeMetricsSnapshot(BaseModel):
    """One API-process resource sample captured during an experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    captured_at: datetime
    process_id: PositiveInteger
    process_cpu_seconds: NonNegativeFloat
    process_max_rss_bytes: NonNegativeInteger
    python_thread_count: PositiveInteger
    logical_cpu_count_available: PositiveInteger
    gil_enabled: bool
    host_logical_cpu_times: tuple[LogicalCpuTimes, ...]
    host_memory_total_bytes: NonNegativeInteger | None
    host_memory_available_bytes: NonNegativeInteger | None
    database_pool: DatabasePoolMetrics | None


def _pool_value(database_engine: Engine, method_name: str) -> int | None:
    method = getattr(database_engine.pool, method_name, None)
    return int(method()) if callable(method) else None


def _pool_max_overflow(database_engine: Engine) -> int | None:
    value = getattr(database_engine.pool, "_max_overflow", None)
    return int(value) if isinstance(value, int) and value >= 0 else None


def _maximum_resident_memory_bytes(maximum_resident_memory: int) -> int:
    # macOS reports bytes; Linux reports kibibytes.
    return (
        maximum_resident_memory
        if sys.platform == "darwin"
        else maximum_resident_memory * 1024
    )


def _logical_cpu_count_available() -> int:
    affinity = getattr(os, "sched_getaffinity", None)
    if callable(affinity):
        try:
            return max(1, len(affinity(0)))
        except OSError:
            pass
    return os.cpu_count() or 1


def _gil_enabled() -> bool:
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    return bool(is_gil_enabled()) if callable(is_gil_enabled) else True


def parse_linux_logical_cpu_times(
    proc_stat: str,
    *,
    clock_ticks_per_second: int,
) -> tuple[LogicalCpuTimes, ...]:
    """Parse cumulative per-core counters from Linux ``/proc/stat``."""
    samples = []
    for line in proc_stat.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        index_text = fields[0].removeprefix("cpu")
        if not index_text.isdigit():
            continue
        counters = tuple(int(value) for value in fields[1:])
        if len(counters) < 4:
            continue
        total_ticks = sum(counters)
        idle_ticks = counters[3] + (counters[4] if len(counters) > 4 else 0)
        samples.append(
            LogicalCpuTimes(
                cpu_index=int(index_text),
                total_seconds=total_ticks / clock_ticks_per_second,
                idle_seconds=idle_ticks / clock_ticks_per_second,
            )
        )
    return tuple(samples)


def parse_linux_memory_bytes(
    proc_meminfo: str,
) -> tuple[int | None, int | None]:
    """Return total and available bytes from Linux ``/proc/meminfo``."""
    values: dict[str, int] = {}
    for line in proc_meminfo.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in {"MemTotal:", "MemAvailable:"}:
            values[fields[0]] = int(fields[1]) * 1024
    return values.get("MemTotal:"), values.get("MemAvailable:")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _host_measurements(
    *,
    proc_stat_path: Path = Path("/proc/stat"),
    proc_meminfo_path: Path = Path("/proc/meminfo"),
) -> tuple[tuple[LogicalCpuTimes, ...], int | None, int | None]:
    cpu_times: tuple[LogicalCpuTimes, ...] = ()
    proc_stat = _read_text(proc_stat_path)
    if proc_stat is not None:
        try:
            clock_ticks = int(os.sysconf("SC_CLK_TCK"))
            cpu_times = parse_linux_logical_cpu_times(
                proc_stat,
                clock_ticks_per_second=clock_ticks,
            )
        except (OSError, ValueError):
            pass

    total_memory = None
    available_memory = None
    proc_meminfo = _read_text(proc_meminfo_path)
    if proc_meminfo is not None:
        try:
            total_memory, available_memory = parse_linux_memory_bytes(
                proc_meminfo
            )
        except ValueError:
            pass
    return cpu_times, total_memory, available_memory


def capture_runtime_metrics(
    database_engine: Engine | None = default_engine,
) -> RuntimeMetricsSnapshot:
    """Capture one aligned process, host, and SQLAlchemy-pool sample."""
    process_usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_times, total_memory, available_memory = _host_measurements()
    return RuntimeMetricsSnapshot(
        captured_at=datetime.now(UTC),
        process_id=os.getpid(),
        process_cpu_seconds=process_usage.ru_utime + process_usage.ru_stime,
        process_max_rss_bytes=_maximum_resident_memory_bytes(
            process_usage.ru_maxrss
        ),
        python_thread_count=active_count(),
        logical_cpu_count_available=_logical_cpu_count_available(),
        gil_enabled=_gil_enabled(),
        host_logical_cpu_times=cpu_times,
        host_memory_total_bytes=total_memory,
        host_memory_available_bytes=available_memory,
        database_pool=(
            DatabasePoolMetrics(
                checked_out=_pool_value(database_engine, "checkedout"),
                checked_in=_pool_value(database_engine, "checkedin"),
                pool_size=_pool_value(database_engine, "size"),
                overflow=_pool_value(database_engine, "overflow"),
                max_overflow=_pool_max_overflow(database_engine),
            )
            if database_engine is not None
            else None
        ),
    )
