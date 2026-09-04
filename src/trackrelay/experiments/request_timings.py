"""Per-request ingestion evidence and ten-second displays, not p95-of-p95s."""

from collections import defaultdict
from json import loads
from math import floor

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class IngestionTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observed_at: AwareDatetime
    step: str
    sequence: int = Field(ge=0)
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    status: int = Field(ge=0, le=599)


def percentile95(values):
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * 0.95
    lower = floor(rank)
    return ordered[lower] + (
        ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]
    ) * (rank - lower)


def read_request_timings(path):
    timings = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            point = loads(line)
            if (
                point.get("type") != "Point"
                or point.get("metric") != "http_req_duration"
            ):
                continue
            data = point["data"]
            tags = data["tags"]
            if tags.get("name") != "POST partner event":
                continue
            timings.append(
                IngestionTiming(
                    observed_at=data["time"],
                    duration_ms=data["value"],
                    step=tags["step"],
                    sequence=int(tags["sample_sequence"]),
                    status=int(tags["status"]),
                )
            )
    return tuple(timings)


def timings_complete(timings, definition):
    expected = {
        (step.name, index)
        for step in definition.steps
        for index in range(step.expected_request_count)
    }
    return (
        len(timings) == len(expected)
        and {(item.step, item.sequence) for item in timings} == expected
    )


def timing_windows(timings, started_at):
    groups = defaultdict(list)
    for item in timings:
        bucket = 10 * floor((item.observed_at - started_at).total_seconds() / 10)
        groups[bucket].append(item)
    return [
        {
            "seconds_after_start": bucket,
            "sample_count": len(items),
            "p95_ms": percentile95(item.duration_ms for item in items),
            "errors": sum(item.status != 201 for item in items),
        }
        for bucket, items in sorted(groups.items())
    ]
