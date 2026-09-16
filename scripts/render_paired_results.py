"""Validate paired evidence, export public numeric data, and render with AWS off."""

from argparse import ArgumentParser
from json import dumps, loads
from pathlib import Path

from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from trackrelay.aws_elasticity_report import (
    ElasticityComparisonReport,
    build_comparison,
    load_comparison_evidence,
)
from trackrelay.experiments.request_timings import timing_windows

OUTPUT = Path(__file__).resolve().parents[1] / "docs/assets/paired"


def export_data(root: Path) -> dict:
    fixed, elastic, journal, verified, hashes = load_comparison_evidence(root)
    saved = ElasticityComparisonReport.model_validate_json(
        (root / "elasticity/report/comparison-report.json").read_text()
    )
    rebuilt = build_comparison(
        fixed,
        elastic,
        session_id=journal["session_id"],
        region=journal["region"],
        git_revision=journal["git_revision"],
        teardown_verified_at=verified,
        source_sha256=hashes,
        now=lambda: saved.generated_at,
        rate_rule="consecutive",
    )
    phase = journal["elasticity_session"]
    if (
        rebuilt != saved
        or not rebuilt.elasticity_demonstrated
        or phase["phase"] != "cloud_complete"
        or phase.get("failed_phase")
        or any(
            phase.get(k)
            for k in ("workflow_error", "cleanup_errors", "diagnostic_errors")
        )
    ):
        raise ValueError(
            "Publication requires matching qualified report and completed journal"
        )
    data = {
        "schema_version": 1,
        "kind": "paired-worker-elasticity",
        "session_id": rebuilt.session_id,
        "measurement_date": fixed.measurement.load_started_at.strftime("%d %B %Y"),
        "application_revision": rebuilt.git_revision,
        "teardown_verified_at": verified.isoformat(),
        "inventory_categories_zero": 30,
        "method": rebuilt.method,
        "comparison_multiplier": rebuilt.observed_step_rate_multiplier,
        "source_sha256": hashes,
        "steps": [],
        "treatments": {},
    }
    elapsed = 0
    for step in fixed.measurement.definition.steps:
        data["steps"].append(
            {
                "name": step.name,
                "start": elapsed,
                "end": elapsed + step.duration_seconds,
                "rate": step.offered_rate_per_second,
            }
        )
        elapsed += step.duration_seconds
    for name, summary, report in (
        ("fixed", fixed, rebuilt.fixed),
        ("elastic", elastic, rebuilt.elastic),
    ):
        result = summary.measurement
        # Explicit numeric selection: never copy AWS responses, logs, or endpoints.
        data["treatments"][name] = {
            "observations": [
                {
                    "seconds": p.seconds_after_load_started,
                    "running": p.worker_running_count,
                    "desired": p.worker_desired_count,
                    "pending": p.worker_pending_count,
                    "accepted": p.database_persisted_events,
                    "completed": p.completed_delivery_events,
                    "outstanding": p.database_persisted_events
                    - p.completed_delivery_events,
                }
                for p in result.observations
            ],
            "latency_windows": timing_windows(
                result.request_timings, result.load_started_at
            ),
            "ingestion_steps": [
                p.model_dump(mode="json") for p in result.ingestion_steps
            ],
            "step_support": [p.model_dump(mode="json") for p in report.step_results],
            "highest_supported_rate": report.highest_supported_rate_per_second,
            "timings": report.timings.model_dump(mode="json")
            if report.timings
            else None,
            "post_load_drain_begins_seconds": report.stable_drain_seconds_after_load,
            "post_load_drain_confirmed_seconds": report.drain_confirmed_seconds_after_load,
            "max_oldest_message_age_seconds": report.maximum_native_oldest_age_seconds,
            "reconciliation": {
                k: getattr(result.reconciliation, k)
                for k in (
                    "generated",
                    "accepted",
                    "processed",
                    "failed",
                    "pending",
                    "unaccounted",
                    "simulator_unique_events",
                    "duplicate_business_effects",
                    "incorrect_final_shipment_states",
                    "successful_retry_attempts",
                )
            },
            "dropped_iterations": result.dropped_iteration_count,
        }
    return data


def sampled(points, key):
    x, y, previous = [], [], None
    for point in points:
        if previous is not None and point["seconds"] - previous > 30:
            x.append(float("nan"))
            y.append(float("nan"))
        x.append(point["seconds"] / 60)
        y.append(point[key])
        previous = point["seconds"]
    return x, y


def render(data: dict, output: Path) -> None:
    figure = Figure(figsize=(13, 11), layout="constrained", facecolor="white")
    FigureCanvasAgg(figure)
    axes = figure.subplots(4, 2, sharex=True, sharey="row")
    end = (
        max(t["observations"][-1]["seconds"] for t in data["treatments"].values()) / 60
    )
    duration = data["steps"][-1]["end"] / 60
    recovery = data["steps"][-1]["start"] / 60
    for column, (name, color) in enumerate(
        (("fixed", "#526e8d"), ("elastic", "#008779"))
    ):
        t = data["treatments"][name]
        points = t["observations"]
        axes[0, column].stairs(
            [s["rate"] for s in data["steps"]] + [0],
            [s["start"] / 60 for s in data["steps"]] + [duration, end],
            color=color,
            linewidth=2,
        )
        axes[0, column].set_title(
            "Fixed · one worker" if name == "fixed" else "Elastic · 1 → 8 → 1",
            loc="left",
            weight="bold",
        )
        axes[1, column].step(
            *sampled(points, "running"), where="post", color=color, linewidth=2
        )
        axes[1, column].step(
            *sampled(points, "desired"),
            where="post",
            color="#999999",
            linestyle="--",
            label="Desired",
        )
        axes[2, column].plot(*sampled(points, "outstanding"), color=color, linewidth=2)
        peak = max(points, key=lambda p: p["outstanding"])
        axes[2, column].annotate(
            f"Maximum observed: {peak['outstanding']:,}",
            (peak["seconds"] / 60, peak["outstanding"]),
            xytext=(8, 12),
            textcoords="offset points",
            fontsize=10,
        )
        axes[2, column].axhline(
            1500, color="#9b6543", linestyle=":", label="Step backlog limit: 1,500"
        )
        windows = t["latency_windows"]
        axes[3, column].plot(
            [w["seconds_after_start"] / 60 for w in windows],
            [w["p95_ms"] for w in windows],
            color=color,
            label="10s ingestion p95",
        )
        axes[3, column].axhline(
            500, color="#b94747", linestyle="--", label="500 ms phase SLO"
        )
        axes[3, column].set_xlabel("Minutes from each workload's start")
        for row in range(4):
            ax = axes[row, column]
            ax.axvspan(recovery, duration, color="#edf5ee", zorder=-2)
            ax.axvspan(duration, end, color="#f1f2f4", zorder=-2)
            ax.set_xlim(0, end)
            ax.set_ylim(bottom=0)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", color="#e1e5e9")
        axes[1, column].legend(loc="upper right", fontsize=8)
        axes[2, column].legend(loc="upper right", fontsize=8)
        axes[3, column].legend(loc="upper right", fontsize=8)
    for row, label in enumerate(
        (
            "Offered events/s",
            "Running workers",
            "Unfinished events",
            "Ingestion p95 (ms)",
        )
    ):
        axes[row, 0].set_ylabel(label)
    axes[1, 0].set_ylim(0, 9)
    maxima = [
        max(p["outstanding"] for p in data["treatments"][name]["observations"])
        for name in ("fixed", "elastic")
    ]
    axes[2, 0].set_ylim(0, max(1800, max(maxima) * 1.18))
    axes[3, 0].set_ylim(
        0,
        max(
            550,
            max(
                w["p95_ms"]
                for t in data["treatments"].values()
                for w in t["latency_windows"]
            )
            * 1.1,
        ),
    )
    title = (
        "Same demand, less unfinished work with autoscaling"
        if maxima[1] < maxima[0]
        else "Fixed and elastic workers under the same demand"
    )
    count = data["treatments"]["fixed"]["reconciliation"]["processed"]
    figure.suptitle(
        f"{title}\nOne paired AWS experiment · {data['measurement_date']} · both runs delivered all {count:,} events",
        fontsize=15,
    )
    multiplier = data["comparison_multiplier"]
    capacity = (
        "No capacity multiplier established."
        if multiplier is None
        else f"Observed supported-step ratio: {multiplier:g}×; not maximum capacity."
    )
    failures = any(
        not step["supported"]
        for treatment in data["treatments"].values()
        for step in treatment["step_support"]
    )
    status = (
        "Some load steps failed." if failures else "All reported load steps passed."
    )
    figure.supxlabel(
        f"Green: low-demand recovery · Gray: post-load observation\nValid paired experiment; {status} {capacity}\n"
        "Latency line: 10s display; pass/fail uses full-phase p95. Lines connect samples, not continuous measurements.",
        fontsize=9,
    )
    output.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        metadata = {"Creator": "TrackRelay offline paired renderer"}
        if ext == "svg":
            metadata["Date"] = None
        with rc_context({"svg.hashsalt": "trackrelay-paired"}):
            figure.savefig(output / f"comparison.{ext}", dpi=170, metadata=metadata)
    figure.clear()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    output = args.output_dir
    if args.session_dir:
        data = export_data(args.session_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "data.json").write_text(dumps(data, indent=2) + "\n")
    else:
        data = loads((output / "data.json").read_text())
    render(data, output)


if __name__ == "__main__":
    main()
