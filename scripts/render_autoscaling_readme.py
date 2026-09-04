"""Export a public numeric subset of one qualified diagnostic and plot it offline.

No AWS calls. Without --session-dir, regenerate figures from committed data.json.
The public subset deliberately excludes raw AWS responses, endpoints and logs.
"""

from argparse import ArgumentParser
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from trackrelay.aws_elasticity_diagnostic import ElasticityDiagnosticSummary
from trackrelay.aws_elasticity_report import REQUIRED_NATIVE_INVENTORY
from trackrelay.experiments.request_timings import timing_windows


def export_data(session_dir: Path) -> dict:
    paths = {
        "summary": session_dir / "elasticity/diagnostic/elastic/summary.json",
        "journal": session_dir / "session.json",
        "inventory": session_dir / "aws-native-inventory-after-destroy.json",
    }
    raw = {key: path.read_bytes() for key, path in paths.items()}
    summary = ElasticityDiagnosticSummary.model_validate_json(raw["summary"])
    journal, inventory = loads(raw["journal"]), loads(raw["inventory"])
    phase = journal["elasticity_diagnostic_session"]
    result = summary.measurement
    if (
        not summary.qualification.qualified
        or journal["status"] != "teardown_verified"
        or not journal.get("teardown_verified_at")
        or phase["phase"] != "cloud_complete"
        or phase.get("workflow_error")
        or phase.get("cleanup_errors")
        or phase.get("diagnostic_errors")
        or phase.get("failed_phase")
        or journal["elastic_diagnostic"]["test_run_id"] != str(result.test_run_id)
        or not journal["elastic_diagnostic"]["qualified"]
        or set(inventory) != REQUIRED_NATIVE_INVENTORY
        or any(value != 0 for value in inventory.values())
    ):
        raise ValueError("Publication requires a qualified, completed, torn-down run")
    if not result.request_timings:
        raise ValueError("Publication requires raw ingestion request timings")

    elapsed = 0
    steps = []
    for step in result.definition.steps:
        steps.append(
            {
                "name": step.name,
                "start_seconds": elapsed,
                "end_seconds": elapsed + step.duration_seconds,
                "events_per_second": step.offered_rate_per_second,
            }
        )
        elapsed += step.duration_seconds
    observations = [
        {
            "seconds": p.seconds_after_load_started,
            "workers_running": p.worker_running_count,
            "workers_desired": p.worker_desired_count,
            "workers_pending": p.worker_pending_count,
            "accepted": p.database_persisted_events,
            "completed": p.completed_delivery_events,
            "outstanding": max(
                0, p.database_persisted_events - p.completed_delivery_events
            ),
            "queue_work": p.source_queue_work,
            "dlq": p.dead_letter_queue_messages,
        }
        for p in result.observations
    ]
    native = {
        series.query_id: [
            {
                "seconds": (
                    p.interval_started_at - result.load_started_at
                ).total_seconds(),
                "value": p.value,
            }
            for p in series.datapoints
        ]
        for series in summary.cloudwatch.series
    }
    return {
        "schema_version": 1,
        "kind": "standalone-elasticity-demonstration",
        "comparison_multiplier": None,
        "session_id": journal["session_id"],
        "region": journal["region"],
        "application_revision": journal["elastic_diagnostic"]["git_revision"],
        "workload": result.definition.name,
        "policy_version": summary.transition.policy.policy_version,
        "quiet_seconds": summary.transition.policy.scale_in_quiet_seconds,
        "load_started_at": result.load_started_at.isoformat(),
        "duration_seconds": result.definition.duration_seconds,
        "teardown_verified_at": journal["teardown_verified_at"],
        "native_inventory": inventory,
        "source_sha256": {key: sha256(value).hexdigest() for key, value in raw.items()},
        "steps": steps,
        "observations": observations,
        "latency_windows": timing_windows(
            result.request_timings, result.load_started_at
        ),
        "ingestion_steps": [p.model_dump(mode="json") for p in result.ingestion_steps],
        "native_metrics": native,
        "qualification": summary.qualification.model_dump(mode="json"),
        "reconciliation": result.reconciliation.model_dump(mode="json"),
        "dropped_iterations": result.dropped_iteration_count,
        "k6_exit_code": result.k6_exit_code,
        "drain_stability_confirmed": result.drain_stability_confirmed,
    }


def sampled(points, key, max_gap=30):
    """Join samples as a visual guide, breaking across unobserved wide gaps."""
    times, values = [], []
    previous = None
    for point in points:
        if previous is not None and point["seconds"] - previous > max_gap:
            times.append(float("nan"))
            values.append(float("nan"))
        times.append(point["seconds"] / 60)
        values.append(point[key])
        previous = point["seconds"]
    return times, values


def render_figures(data: dict, output: Path) -> None:
    blue, orange, ink = "#2563a6", "#bc551d", "#172b40"
    duration = data["duration_seconds"] / 60
    end = data["observations"][-1]["seconds"] / 60
    recovery = data["steps"][-1]["start_seconds"] / 60
    points = data["observations"]

    def canvas(title, subtitle):
        figure = Figure(figsize=(10, 6.1), facecolor="white")
        FigureCanvasAgg(figure)
        axes = figure.subplots(2, 1, sharex=True)
        figure.subplots_adjust(
            left=0.12, right=0.97, top=0.79, bottom=0.15, hspace=0.40
        )
        figure.text(0.12, 0.95, title, fontsize=20, weight="bold", color=ink)
        figure.text(0.12, 0.90, subtitle, fontsize=11, color="#425466")
        for axis in axes:
            axis.set_facecolor("white")
            axis.spines[["top", "right"]].set_visible(False)
            axis.spines[["left", "bottom"]].set_color("#bcc7d2")
            axis.tick_params(colors=ink, labelsize=11)
            axis.grid(axis="y", color="#e4e9ee", linewidth=0.7)
            axis.set_axisbelow(True)
            axis.axvspan(recovery, duration, color="#edf6f1", zorder=-2)
            axis.axvspan(duration, end, color="#f1f3f6", zorder=-2)
            axis.axvline(duration, color="#8c99a5", linewidth=0.8)
            axis.set_xlim(0, end)
            axis.set_ylim(bottom=0)
        axes[-1].set_xlabel("Minutes from workload start", fontsize=11, color=ink)
        axes[-1].set_xticks(range(0, int(end) + 1, 2))
        figure.text(
            0.12,
            0.04,
            "Green: low-demand recovery  ·  Gray: post-load drain verification",
            fontsize=10,
            color="#425466",
        )
        return figure, axes

    def save(figure, name):
        for extension in ("png", "svg"):
            metadata = {"Creator": "TrackRelay offline evidence renderer"}
            if extension == "svg":
                metadata["Date"] = None
            with rc_context({"svg.hashsalt": "trackrelay-autoscaling"}):
                figure.savefig(
                    output / f"{name}.{extension}", dpi=180, metadata=metadata
                )
        figure.clear()

    figure, (demand, workers) = canvas(
        "More demand. More workers. Then fewer again.",
        "One AWS run · 25 events/s at peak · 1 → 8 → 1 running workers",
    )
    edges = [step["start_seconds"] / 60 for step in data["steps"]] + [duration, end]
    rates = [step["events_per_second"] for step in data["steps"]] + [0]
    demand.stairs(rates, edges, color=blue, linewidth=2.6, baseline=None)
    demand.set_ylabel("Offered events / s", fontsize=11, color=ink)
    demand.set_ylim(0, max(rates) * 1.25)
    demand.set_yticks([0, 5, 10, 25])
    demand.text(2.15, 27.5, "25/s for 3 minutes", fontsize=11, color=ink)
    demand.text(6.6, 4.5, "Demand returns to 1/s", fontsize=11, color=ink)
    workers.step(
        *sampled(points, "workers_desired"),
        where="post",
        color="#8794a1",
        linestyle="--",
        linewidth=1.5,
        label="Desired",
    )
    workers.step(
        *sampled(points, "workers_running"),
        where="post",
        color=orange,
        linewidth=2.6,
        label="Running (sampled)",
    )
    workers.set_ylabel("Worker tasks", fontsize=11, color=ink)
    workers.set_ylim(0, 10.5)
    workers.set_yticks([0, 1, 4, 8])
    workers.legend(loc="upper right", fontsize=10, frameon=False)
    expanded = next(p["seconds"] / 60 for p in points if p["workers_running"] == 8)
    contracted = next(
        p["seconds"] / 60
        for p in points
        if p["seconds"] / 60 >= recovery and p["workers_running"] == 1
    )
    for x, y, label, offset in (
        (expanded, 8, f"8 running at {expanded:.2f} min", (18, 12)),
        (contracted, 1, f"Back to 1 at {contracted:.2f} min", (12, 34)),
    ):
        workers.annotate(
            label,
            xy=(x, y),
            xytext=offset,
            textcoords="offset points",
            fontsize=10,
            color=ink,
            arrowprops={"arrowstyle": "-", "color": ink},
        )
    save(figure, "demand-workers")

    figure, (latency, backlog) = canvas(
        "Fast acceptance. A backlog that clears.",
        "5,730 events delivered · zero request errors · zero duplicate business effects",
    )
    windows = data["latency_windows"]
    # Horizontal segments show actual ten-second buckets; no p95 interpolation.
    for window in windows:
        x = window["seconds_after_start"] / 60
        latency.hlines(window["p95_ms"], x, x + 10 / 60, color=blue, linewidth=2.5)
    latency.axhline(500, color="#9d3838", linestyle="--", linewidth=1.1)
    latency.text(0.15, 523, "500 ms phase-p95 limit", fontsize=10, color="#9d3838")
    latency.set_ylabel("Ingestion p95 (ms)", fontsize=11, color=ink)
    latency.set_ylim(0, max(610, max(w["p95_ms"] for w in windows) * 1.2))
    latency.set_yticks([0, 100, 250, 500])
    latency.text(
        0.98,
        0.78,
        "10-second buckets; qualification uses full phases",
        transform=latency.transAxes,
        ha="right",
        fontsize=10,
        color=ink,
    )
    spike = max(windows, key=lambda w: w["p95_ms"])
    latency.annotate(
        f"Brief {spike['p95_ms']:.0f} ms bucket",
        xy=((spike["seconds_after_start"] + 5) / 60, spike["p95_ms"]),
        xytext=(22, 15),
        textcoords="offset points",
        fontsize=10,
        color=ink,
        arrowprops={"arrowstyle": "-", "color": ink},
    )
    backlog.plot(
        *sampled(points, "outstanding"),
        color=orange,
        linewidth=2,
        marker=".",
        markersize=3,
    )
    backlog.set_ylabel("Unfinished events", fontsize=11, color=ink)
    maximum = max(points, key=lambda p: p["outstanding"])
    backlog.set_ylim(0, maximum["outstanding"] * 1.4)
    backlog.annotate(
        f"Peak: {maximum['outstanding']} unfinished",
        xy=(maximum["seconds"] / 60, maximum["outstanding"]),
        xytext=(18, 14),
        textcoords="offset points",
        fontsize=11,
        color=ink,
        arrowprops={"arrowstyle": "-", "color": ink},
    )
    backlog.text(
        0.98,
        0.8,
        "Sampled: accepted minus completed\nConfigured backlog limit: 1,500",
        transform=backlog.transAxes,
        ha="right",
        fontsize=10,
        color=ink,
    )
    save(figure, "latency-backlog")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("docs/assets/autoscaling")
    )
    args = parser.parse_args()
    data = (
        export_data(args.session_dir)
        if args.session_dir
        else loads((args.output_dir / "data.json").read_text())
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    render_figures(data, args.output_dir)
    if args.session_dir:
        (args.output_dir / "data.json").write_text(dumps(data, indent=2) + "\n")
    print(f"Rendered standalone elasticity figures in {args.output_dir}; no AWS calls")


if __name__ == "__main__":
    main()
