# Workload v6: bounded driver headroom and short-window recovery evidence

## Current policy amendment: two-minute quiet period

New deployments now pair this **unchanged workload v6** with **policy v6**.
The sole scaling change is the `QuietSeconds` threshold: **120 seconds**, down
from policy v5's 180 seconds. Older policy evidence retains its original
threshold. The original workload-v6 specification below describes its first
policy-v5 pairing; this amendment supersedes that pairing for fresh deployments.

Scale-in still requires continuously low accepted demand, unfinished events and
all three SQS work counts. Busy, failed or stale samples reset the timer, and
missing metrics cannot authorize contraction. Scale-out remains two ten-second
arrival-rate buckets >=3/s requesting eight workers; renewed demand is not
required to wait for another quiet period. Both cooldowns remain 60 seconds.
Detection, action and ECS task startup/shutdown add latency: 120 seconds is a
quiet qualification threshold, not a guaranteed task-stop deadline.

The 630-second workload, 25/s peak, 60-second observed recovery requirement,
180-second stable-empty post-load drain, backlog/age/SLO/correctness gates and
eight-worker maximum are unchanged. No extra traffic or observation time was
added. Previous failed runs remain failed; this document does not authorize AWS
spending. The separate diagnostic-log atomicity issue is not part of this change.

## Original workload-v6 specification

New diagnostic and paired runs select `aws-elasticity-demo-v6` automatically.
The [v5 contract](aws-elasticity-v5-contract.md) remains the historical record;
v6 inherits its 630-second, 5,730-event waveform, 25/s peak, policy v5, 60-second
expanded-peak requirement, latency/correctness/headroom bounds and drain rules.
Only the changes below apply prospectively. No earlier failure is reclassified.

## Driver headroom, not additional offered traffic

Each plateau preallocates five virtual users per offered event/s, capped at that
same number: 125 VUs for 25/s. There is no runtime VU expansion, backfill, retry,
or extra event scheduling. Historical v5 retains one VU per event/s.
The five-second concurrency budget provides margin over the latest 4.3-second
stall; it is a bounded test-driver configuration, not a promise to tolerate
arbitrarily slow responses. Zero dropped iterations, exact request counts and
complete raw records remain mandatory. More VUs do not repair the server stall
or relax the phase p95 <500 ms requirement.

This follows [k6's preallocation guidance](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/arrival-rate-vu-allocation/).
Review local driver CPU/memory and connection overhead; the extra VUs run in
the local driver, not as AWS worker tasks. Both paired treatments use the same
allocation. No cloud capacity or cost ceiling changes here.

## Recovery evidence

V6 requires a continuous live ECS **service-count** suffix of at least 60 seconds
at desired=running=1, pending=0, during recovery. Its last sample must reach
within 30 seconds of scheduled load end, and the existing maximum 30-second
observation-gap rule still applies. The final post-load observation must also
be at minimum. Native worker count must corroborate the return: the latest
native point timestamped within the live suffix and before scheduled load end
must show one worker. Missing, contradictory final, or only post-load native
evidence cannot qualify. A partial final native bucket is allowed; it is not
claimed to represent a complete minute at minimum.

Earlier versions still require their complete native minute. V6 retains native
data at its true resolution; no interpolation or fabricated points are used.
The report method becomes `short-plateau-completion-v3` to identify the changed
recovery qualification; the per-step completion calculation itself is unchanged.

Service counts, Container Insights samples and scaling-activity completion are
different observations. Retained scaling activities and task diagnostics help
explain shutdown delay. A service-count return does **not** prove all retiring
containers had already exited, or that their billing had ended. Do not use this
timing as a billed-cost saving measurement.

## Locating the ingestion stall

One structured `request_timing` record now includes `partner_lookup`,
`persistence`, and `publication` spans, each with start offset, duration and
success/error outcome. `publication` includes outbox publication and its queue
operation; it does not isolate every SQL statement or SDK retry. A random local
trace ID links the spans within their record, without business IDs, payloads,
raw paths, or exception text. `unattributed_ms` covers everything outside those
spans (including scheduling/validation/response work); it is not a measurement
of threadpool waiting alone. Stage timings cross the synchronous handler's
threadpool and remain isolated between concurrent requests.

These are diagnostics, not additional custom metric identities or SLO gates.
They are collected by the existing endpoint timing-log collector. This change
does not claim to fix the underlying 4.3-second stall: the next run should identify
which stage accounts for it if it recurs.

## Local regression and next run

```shell
make test
make lint
make elasticity-driver-check
uv run --locked python scripts/check-elasticity-driver-stall.py \
  --output-directory results/local-elasticity-stall-UNIQUE_ID
```

The last command uses a loopback receiver, the pinned k6 image and the production
driver. Two 20-second, peak-only probes inject a 4.3-second stall: v5 must expose
dropped arrivals, while v6 must send all 500 unique requests with no drops.
It retains both failures/latency spikes. This is not the full waveform, an
application capacity test, or cloud qualification evidence.

Use the unchanged [diagnostic runbook](aws-elasticity-diagnostic-runbook.md)
with a fresh run identity, plan review, current credentials and explicit approval.
Rebuild the API image. No AWS retry is authorized by this document.
