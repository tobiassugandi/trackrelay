"""Public report integrity and offline rendering; no live AWS evidence needed."""

from json import dumps, loads
from math import isnan
from pathlib import Path

from pytest import mark, raises

from scripts.render_autoscaling_readme import export_data, render_figures, sampled
from tests.test_aws_elasticity_diagnostic import diagnostic_summary
from trackrelay.aws_elasticity_report import REQUIRED_NATIVE_INVENTORY

PUBLIC_DATA = Path(__file__).resolve().parents[1] / "docs/assets/autoscaling/data.json"


def test_public_data_supports_the_readme_without_a_comparison_claim():
    data = loads(PUBLIC_DATA.read_text())
    assert data["comparison_multiplier"] is None
    assert data["qualification"]["qualified"]
    assert data["qualification"]["rejection_reasons"] == []
    assert data["reconciliation"]["invariants_passed"]
    assert data["reconciliation"]["accepted"] == 5730
    assert data["reconciliation"]["simulator_unique_events"] == 5730
    assert data["reconciliation"]["duplicate_business_effects"] == 0
    assert data["reconciliation"]["unaccounted"] == 0
    assert data["dropped_iterations"] == data["k6_exit_code"] == 0
    assert data["drain_stability_confirmed"]
    assert sum(w["sample_count"] for w in data["latency_windows"]) == 5730
    assert (
        sum(
            (s["end_seconds"] - s["start_seconds"]) * s["events_per_second"]
            for s in data["steps"]
        )
        == 5730
    )
    assert all(p["p95_response_latency_ms"] < 500 for p in data["ingestion_steps"])
    # Preserve the inconvenient short-window spike, not just favorable phase p95s.
    assert round(max(w["p95_ms"] for w in data["latency_windows"])) == 572
    assert len(data["native_metrics"]) == 25
    assert set(data["native_inventory"]) == REQUIRED_NATIVE_INVENTORY
    assert all(v == 0 for v in data["native_inventory"].values())
    text = PUBLIC_DATA.read_text()
    for private_marker in (
        "arn:aws:",
        "amazonaws.com",
        "api_runtime",
        "raw_payload",
        "account_id",
        "access_key",
    ):
        assert private_marker not in text


def test_sampling_breaks_gaps_without_inventing_measurements():
    times, values = sampled(
        [
            {"seconds": 0, "count": 1},
            {"seconds": 10, "count": 8},
            {"seconds": 41, "count": 1},
        ],
        "count",
    )
    assert len(times) == len(values) == 4
    assert isnan(times[2]) and isnan(values[2])
    assert values[:2] == [1, 8] and values[-1] == 1


def test_public_dataset_renders_both_png_and_svg_offline(tmp_path):
    render_figures(loads(PUBLIC_DATA.read_text()), tmp_path)
    for name in ("demand-workers", "latency-backlog"):
        assert (tmp_path / f"{name}.png").read_bytes().startswith(b"\x89PNG")
        svg = (tmp_path / f"{name}.svg").read_text()
        assert "<svg" in svg and len(svg) > 1000


@mark.parametrize("failure", ["journal", "inventory", "missing_category", "run_id"])
def test_export_rejects_incomplete_or_mismatched_publication(tmp_path, failure):
    summary = diagnostic_summary()
    evidence = tmp_path / "elasticity/diagnostic/elastic"
    evidence.mkdir(parents=True)
    (evidence / "summary.json").write_text(summary.model_dump_json(round_trip=True))
    journal = {
        "status": "teardown_verified",
        "teardown_verified_at": "2026-09-04T12:48:29+00:00",
        "elasticity_diagnostic_session": {
            "phase": "cloud_complete",
            "cleanup_errors": [],
        },
        "elastic_diagnostic": {
            "test_run_id": str(summary.measurement.test_run_id),
            "qualified": True,
        },
    }
    inventory = dict.fromkeys(REQUIRED_NATIVE_INVENTORY, 0)
    if failure == "journal":
        journal["elasticity_diagnostic_session"]["cleanup_errors"] = ["failed"]
    elif failure == "inventory":
        inventory["ecs_tasks"] = 1
    elif failure == "missing_category":
        inventory.pop("ecs_tasks")
    else:
        journal["elastic_diagnostic"]["test_run_id"] = "wrong-run"
    (tmp_path / "session.json").write_text(dumps(journal))
    (tmp_path / "aws-native-inventory-after-destroy.json").write_text(dumps(inventory))
    with raises(ValueError, match="qualified, completed, torn-down"):
        export_data(tmp_path)
