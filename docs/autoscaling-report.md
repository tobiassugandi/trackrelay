# Autoscaling demo: evidence and chart notes

The [archived standalone README](archive/README-standalone-mvp.md) is the original
short report. The [current README](../README.md) now presents the later paired
experiment; see its [measurement notes](paired-elasticity-report.md). This page
records its sources and measurement boundaries. This is a standalone
demonstration, not the fixed-versus-elastic or async-versus-sync comparison.

## Recorded run

- Session: `cloud-session-4-20260904T121328Z`, AWS `ap-southeast-3` (Jakarta).
- Application revision: `d2ca9d465d19e3c7642a95111a58cce213da5ec0`.
- Workload: `aws-elasticity-demo-v6`; policy v6, one to eight workers.
- Scheduled load: 2026-09-04 12:27:00 UTC, 630 seconds, 5,730 events.
- Shape: `1 → 5 → 10 → 25 → 10 → 5 → 1` events/s for
  `30 → 30 → 30 → 180 → 30 → 30 → 300` seconds respectively.
- Topology: two fixed API tasks, one fixed simulator, private PostgreSQL RDS,
  SQS/DLQ, and one to eight Fargate workers.
- Result: all diagnostic guardrails passed; exact reconciliation, stable drain,
  and teardown verified. All 30 tracked native inventory categories were zero
  at 2026-09-04 12:48:29 UTC.

## What evidence do we have?

| Evidence retained locally | What it tells us | Published subset |
| --- | --- | --- |
| k6 schedule, summaries and 5,730 raw request timings | Offered load, actual request count, phase p95, errors and dropped iterations | Schedule, phase results, ten-second p95 with sample counts |
| Timestamped application, ECS and SQS observations | Desired/running/pending workers, accepted/completed events, queue work and DLQ | Numeric timeline including post-load drain |
| 25 native CloudWatch series | API/worker/simulator utilization, queue depth/age, ALB requests/latency/errors, RDS health | Series IDs, timestamps relative to start and native values |
| High-resolution custom metrics, alarm history and scaling activities | Demand detection, quiet timer, scaling decisions and timing | Policy version; full diagnostics remain local |
| Database/simulator reconciliation | Every event accounted for; retries, duplicates and final-state correctness | Aggregate reconciliation result |
| Terraform plans, deployment identities, session journal and native inventory | Reproducibility, guarded transitions, successful cleanup | Revision, session identity, zero inventory, teardown timestamp and source hashes |
| Request-stage timing logs and ECS diagnostics | Investigation of transient stalls or task failures | Not published; not needed to read the headline |

The [public dataset](assets/autoscaling/data.json) is an explicit numeric export,
not a copy of AWS responses. It excludes account IDs, ARNs, endpoints, IPs,
credentials, request payloads and raw logs. The source files remain under the
Git-ignored local session directory. Their SHA-256 fingerprints are recorded
in the export; fingerprints provide provenance, not independent access to the
unpublished evidence. The subset supports chart reproduction, not a complete
independent rerun of every qualification check.

## How to read the figures

**Demand and workers.** Blue is scheduled offered traffic, not a CloudWatch
arrival-rate estimate. The exact phase request counts, zero dropped iterations
and reconciliation establish that all scheduled events were sent and accepted.
The orange staircase is sampled ECS running-worker count; dashed gray is desired
count. Both use the same time axis. First-observed eight-worker and one-worker
points are at 119.110 and 510.136 seconds, not exact task-transition times.
The last pre-end sample is at 627.795 seconds, giving 117.659 observed seconds
at minimum during recovery. Service counts do not establish when every retiring
container stopped or when its billing ended.

**Latency.** Each blue segment is a p95 calculated directly from raw k6
request durations in a ten-second bucket, located by its recorded timestamp.
These are HTTP acceptance timings, not downstream completion timings.
Buckets contain 4–251 requests, including a partial final bucket. The public
dataset retains each count. No p95 values are averaged together; the actual
500 ms acceptance gate is calculated separately for each full workload phase.
One bucket reaches 572.316 ms; peak-phase p95 is 79.459 ms, and the largest
phase p95 is 124.266 ms. All phases pass, but neither every request nor every
ten-second bucket was below 500 ms. There are no synthetic zero-latency samples
during the post-load period.

**Backlog.** Unfinished events are sampled accepted minus completed delivery
counts, not SQS visible-message count. The sampled maximum is 479, below the
1,500-event guardrail. Native source-queue work peaks at 245 messages and oldest
message age at 20 seconds; these are different series at a different cadence,
not contradictory maxima. Connecting samples is a visual guide, not an inferred
measurement at every instant. Gaps over 30 seconds are broken, not filled.

Green shading marks the five-minute low-demand recovery phase. Gray marks the
post-load period, when the controller verifies stable drain. It extends the
plots beyond the 10½-minute traffic waveform; it is not extra offered load.
Native metrics remain at their original one-minute resolution, while custom
control telemetry runs every ten seconds. No native one-minute series is
upsampled into fictional ten-second evidence.

## Why workers expanded and contracted

The [frozen v6 policy](aws-elasticity-v6-contract.md) requests eight workers
after two ten-second accepted-arrival-rate buckets at or above 3 events/s.
Return to one requires 120 seconds of continuously low demand and low
unfinished/queue work. Failed, stale or busy samples reset the quiet timer.
CloudWatch evaluation and ECS actions add time; the quiet threshold is not a
shutdown deadline. Only worker count changes, not API or RDS capacity.

The cloud benefit is on-demand compute without the project owner procuring
physical peak-capacity servers. It is not infinite or instantaneous capacity,
nor evidence that the whole stack shrinks to zero during quiet periods. See
[AWS's ECS scaling documentation](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html)
and [Fargate overview](https://aws.amazon.com/fargate/).

## Reproduce the figures with AWS off

From the repository root:

```shell
uv sync --locked
uv run --locked python scripts/render_autoscaling_readme.py
```

This reads committed `docs/assets/autoscaling/data.json` and regenerates the
PNG and SVG figures beside it. It makes no AWS calls. To rebuild the numeric
export from the original locally retained evidence:

```shell
uv run --locked python scripts/render_autoscaling_readme.py \
  --session-dir results/aws-sessions/cloud-session-4-20260904T121328Z
```

Export revalidates the diagnostic summary against its measurement, checks the
matching run identity, completed journal and zero native inventory, then selects
the public fields. It does not rewrite the raw summary's `headline_eligible=false`
or invent a comparison multiplier. Historical failures remain failures. Use the
[operator runbook](aws-elasticity-diagnostic-runbook.md) only after reviewing and
approving a fresh billable session; plotting needs no cloud session.
