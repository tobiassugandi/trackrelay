# Short elasticity demonstration: workload v5 / policy v5

This is the pre-data contract for **new** runs. It does not reclassify any previous
failure or authorize AWS spending. The same profile is used by the elastic-only
diagnostic and by both treatments of the final paired experiment. Diagnostic
sessions remain ineligible for a comparison multiplier.

## Workload and claims

| Phase | Events/s | Seconds | Events |
| --- | ---: | ---: | ---: |
| Baseline | 1 | 30 | 30 |
| Rise | 5 | 30 | 150 |
| Rise | 10 | 30 | 300 |
| Peak | 25 | 180 | 4,500 |
| Fall | 10 | 30 | 300 |
| Fall | 5 | 30 | 150 |
| Recovery | 1 | 300 | 300 |

Total: **630 seconds, 5,730 events**. The recovery window is fixed so that paired
runs are identical; there is no adaptive early success. Provisioning, image
builds, migration, minute-aligned start, stable drain and teardown are additional.
The existing 180-second stable-empty drain and 1,200-second deadline remain.

Elastic acceptance additionally requires at least 60 continuously observed
seconds with all eight workers running/desired and none pending **during the
25/s peak**, with no observation gap over 30 seconds. Return to one must still
be observed for at least 60 seconds before recovery ends. Backlog <=1,500,
oldest native message age <=180 seconds, all correctness/headroom/error gates
and unconditional verified teardown remain required.

A passing result demonstrates the bounded waveform, not indefinite production
capacity. An X-times-traffic claim needs comparable measurements of its stated
denominator; a fixed-worker async run is not a synchronous baseline. The report's
`short-plateau-completion-v2` method versions the shortened sampling window; its
per-step completion rule remains conservative: throughput >= offered rate and
non-growing backlog. For the 30-second phases, a sampled window must span at
least 10 seconds; longer phases require at least duration minus 60 seconds, and
all existing coverage checks apply. Every occurrence of a rate must pass.

## Fresh scaling, conservative release

Scale-out stays at 3 accepted unique events/s for two ten-second Maximum buckets,
requesting exactly eight workers. Both API replicas read the global database;
Maximum is intentional, not Sum across replicas.

Every ten seconds each publisher independently samples accepted rate, unfinished
logical events and **all three SQS work counts**. `QuietSeconds` starts at zero
and increases only while rate <2/s, unfinished events <10, and queue work <10.
A busy sample, database/SQS/publication error, restart, or sampling gap >15 seconds
resets the timer. The low alarm uses ten-second **Minimum** `QuietSeconds` across
replicas, threshold 180 seconds. Its expression `FILL(quiet, 0)` treats missing
telemetry as *not safe to contract*, unlike treating missing work as an empty
queue. Both directions retain a 60-second cooldown. No laptop issues scaling
commands. Detection and task startup still take time; deadlines are not promises.

## Latency and reporting

For v5, the ingestion gate is **each phase p95 <500 ms and errors <1%**, measured
by k6 and independently checked from complete per-request records. Every expected
step/sequence must occur exactly once in the samples. No dropped iterations,
missing request records, or invalid timings are allowed. The individual records
are stored in `k6-points.json` and `result.json`; `request-timing-windows.json`
contains ten-second p95 displays, error counts and sample counts. These displays
are not additional low-sample-count SLO gates and are never averaged into p95.

Native ALB p95 remains recorded at its true 60-second resolution, but is
**corroboration rather than the ingestion gate for v5**: its population includes
non-ingestion traffic and its buckets do not match the short phases. This rule
is prospective. V2/v3/v4 keep their original native-latency gate. A native breach
must still be visible in saved metrics/charts and investigated; it is not erased.

The API emits structured timings by route template (no raw partner identifiers,
request bodies or query strings), plus telemetry-query duration. The publisher
batches raw server ingestion durations into high-resolution CloudWatch samples;
it does not publish a p95-of-p95s. The dashboard overlays ten-second accepted rate
and server ingestion p95 alongside native metrics. Server timings are not the
same as client end-to-end timings. The in-memory server timing buffer is bounded
and best-effort; the complete k6 records, not this buffer, qualify ingestion.

Six custom metrics use `TrackRelay/Elasticity`: ArrivalRate, OutstandingEvents,
CompletedEvents (cumulative distinct completions), QuietSeconds,
TelemetryQueryLatency, and ServerIngestionLatency. Both alarms are now high
resolution. Review metric/alarm/API/log costs and database/publisher overhead
before approval. Telemetry is present in both fixed and elastic deployments;
the transition still changes only the five autoscaling resources.

`diagnostics/scaling/TEST_RUN_ID/` retains high-resolution metric data, alarm
histories, scaling activities, endpoint timing logs and telemetry-query logs.
Collection is best-effort and does not replace mandatory native evidence.
Native ALB/SQS/infrastructure data stays native-resolution, with no interpolation.

AWS documents high-resolution query periods in the
[MetricDataQuery reference](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_MetricDataQuery.html).
The release expression and its input both explicitly use ten-second periods.

Run a **fresh** session using the [diagnostic runbook](aws-elasticity-diagnostic-runbook.md)
or [paired runbook](aws-elasticity-runbook.md). No prior session approval carries over.
