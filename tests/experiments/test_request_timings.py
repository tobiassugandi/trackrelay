from datetime import UTC, datetime
from json import dumps

from pytest import raises

from trackrelay.experiments.request_timings import (
    percentile95,
    read_request_timings,
    timing_windows,
)


def test_raw_timing_parser_retains_samples_not_aggregated_percentiles(tmp_path):
    path = tmp_path / "points.json"
    point = {
        "type": "Point",
        "metric": "http_req_duration",
        "data": {
            "time": "2026-09-04T00:00:01Z",
            "value": 12.5,
            "tags": {
                "name": "POST partner event",
                "step": "baseline",
                "sample_sequence": "0",
                "status": "201",
            },
        },
    }
    path.write_text(
        dumps({"type": "Metric", "metric": "http_req_duration"})
        + "\n"
        + dumps(point)
        + "\n"
    )
    samples = read_request_timings(path)
    assert len(samples) == 1 and samples[0].duration_ms == 12.5
    assert timing_windows(samples, datetime(2026, 9, 4, tzinfo=UTC)) == [
        {"seconds_after_start": 0, "sample_count": 1, "p95_ms": 12.5, "errors": 0}
    ]
    assert percentile95([0, 100]) == 95
    assert percentile95([]) is None
    point["data"]["value"] = float("nan")
    path.write_text(dumps(point) + "\n")
    with raises(ValueError):
        read_request_timings(path)
