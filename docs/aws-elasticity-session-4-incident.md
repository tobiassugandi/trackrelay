# Session 4 fixed-control interruption — 2026-09-03

Session: `cloud-session-4-20260903T113739Z`

Measured revision: `f4dab6402b1b02c896c5f7c4163ac3e57536ed92`

Region: `ap-southeast-3`

Outcome: invalid Stage 9.6 control; Stage 9.7 never started. No elasticity headline.

## What the retained evidence establishes

- k6 completed all 3,660 scheduled requests, with zero dropped iterations and
  zero request errors. Per-step ingestion p95 was approximately 49–62 ms.
- Delivery completed and the queue drained, but reconciliation found only 3,303
  simulator receipts. Exactly 357 events were unaccounted for downstream.
- ECS stopped the original simulator for `Task failed container health checks`.
  Stopping began at 11:52:47 UTC; the replacement started at 11:54:42 UTC.
  The delivery timeline plateaued at 357 completed deliveries during replacement.
  The simulator's receipt and idempotency stores are process-local memory, so
  replacement loses that evidence. This is not proof that 357 HTTP deliveries
  never happened, and is not a successful end-to-end correctness result.
- A post-teardown CloudWatch recheck confirmed the missing 11:53 UTC simulator
  CPU and memory bucket. The complete-window collector rejects such a gap.
  The original collector exception was not journaled, so its exact historical
  exception is inferred from the collector boundary and the retained/rechecked
  evidence, not recovered from an original traceback.
- Terraform deleted the performance log group at 12:12:47 UTC. CloudTrail showed
  ECS recreating that exact group at 12:13:19 UTC, 32 seconds later. Ownership and
  dependency ordering alone did not prevent late telemetry recreation.
- The original native inventory found one Container Insights log group and
  zero resources in the other 29 categories. The old wrapper raised on cleanup
  failure before finalizing its journal, obscuring the workflow failure.

## Authorized recovery

The user approved diagnosis preservation, implementation fixes, and removal of
the leftover log group, but no new cloud experiment. The recovery preserved
the original manifest and nonzero inventory under `diagnostics/original/`, and
saved non-secret ECS task-stop metadata plus a separately labeled CloudWatch
recheck under the session's `diagnostics/` directory.

After confirming empty Terraform state and native stack absence, recovery
deleted only `/aws/ecs/containerinsights/trackrelay-ba577d53-async/performance`.
Five absent samples spanning over 60 seconds followed. A fresh full verification
found all 30 native categories zero and recorded `teardown_verified` at
2026-09-03 12:36:00 UTC. Deleted CloudWatch logs are not recoverable; the diagnosis
and remaining session artifacts are retained locally. Original measured results
were not changed, and the old workflow journal was not retroactively fabricated.

Private generated artifacts live at:

```text
results/aws-sessions/cloud-session-4-20260903T113739Z/
  elasticity/fixed/result.json
  diagnostics/original/
  diagnostics/ecs-*.json
  diagnostics/cloudwatch-recheck.json
  diagnostics/cloudtrail-log-lifecycle.json
  diagnostics/log-group-before-cleanup.json
  diagnostics/local-*-probe-soak.json
  container-insights-cleanup.json
  aws-native-inventory-after-destroy.json
  session.json
```

## Implementation response

1. Retain safe nested workflow and cleanup failures in the session journal and
   CLI, even when cleanup fails. Collect ECS stopped/running task evidence before
   cleanup; diagnostic failure must never block destruction.
2. Preserve partial native metric responses and collection status. Do not fill
   missing simulator utilization with zero or treat partial evidence as qualified.
3. After successful Terraform destruction and empty state, require native stack
   absence before deleting the exact session performance group. Recheck for late
   recreation and require five consecutive absent samples, 15 seconds apart,
   within 13 checks. Verification remains read-only and separately requires zero
   resources across the full inventory. Unknown leftovers or query failures fail
   closed; the cleanup is not a guarantee against arbitrarily delayed AWS activity.
4. Replace the urllib inline probes with one stdlib-only module, increase the
   HTTP probe budget from 1 to 2 seconds and the container execution budget from
   2 to 5 seconds, with a 30-second startup grace. Keep the interval at 10 seconds
   and the failure threshold at three. Docker and ECS use the same probe.
   Simulator liveness runs directly on the event loop, without waiting for a
   synchronous request-handler thread. Neither capacity nor SLOs are increased.
5. Add a local capacity-limited image soak: 0.25 vCPU, 512 MiB, read-only root,
   temporary writable `/tmp`, real image health checks, and exact receipt counts.
   The old local image reproduced a two-second probe timeout while retaining all
   1,800 events. This supports probe fragility, but the destroyed AWS task's probe
   output is unavailable, so the precise underlying AWS timeout mechanism remains
   unproven. Local Docker runs, including emulated x86_64 runs, are not Fargate
   qualification evidence.

Local validation completed at 10 events/s for 180 seconds per soak:

| Image/probe | Local execution | Receipts | Probe failures | Result |
| --- | --- | --- | --- | --- |
| Old local image / 2-second budget | Native ARM64 | 1,800 / 1,800 | 1 timeout | Failed probe check |
| Revised image / 5-second budget | Native ARM64 | 1,800 / 1,800 | 0 | Passed |
| Exact session image / 2-second budget | Emulated AMD64 | 1,800 / 1,800 | 9 failed checks; became unhealthy | Failed probe check |
| Revised image / 5-second budget | Emulated AMD64 | 1,800 / 1,800 | 0 | Passed |

All three rebuilt production image smoke tests also passed. Terraform validation
and all 17 mocked-provider tests passed; all 564 default Python tests passed.
Regression tests cover receipt
loss rejection, retained partial metric evidence, nested cleanup failures, and
late log recreation. The 15 database integration tests are outside the default
local suite and were not run for this change.

The simulator remains intentionally disposable and in-memory. A replacement
invalidates that treatment; we do not reconstruct missing receipts or silently
add a new persistence service. A fresh reviewed plan, immutable images, session
ID, and explicit spending approval are required to try the cloud experiment
again. The failed fixed result must not be paired with a later elastic run.

## Second attempt: driver boundary failure

Session: `cloud-session-4-20260903T133450Z`, measured revision
`f1dd0910a8bd` (2026-09-03). This is a separate failure, not recurrence of the
simulator receipt loss.

The retained `elasticity/fixed/k6-summary.json` and `k6.log` show 3,663 iterations
and 3,662 HTTP requests for the 3,660-event manifest:

- Baseline issued 61 rather than 60 requests; fall-5 issued 301 rather than 300.
- One acceptance check failed in rise-5 and one in recovery. The global lookup
  allowed earlier steps' extra iterations to consume the next slice's first
  event. The following step then submitted that event again; the API's duplicate
  path returns 200, not the required 201. These retained counts/checks and the
  code establish the cross-slice explanation; response bodies were not retained.
- Recovery attempted a further iteration beyond the entire array and threw
  `scenario recovery exceeded its manifest slice`.
- k6 exited 99 with failed exact-count and acceptance-check thresholds. The
  controller correctly rejected the fixed result as `measurement_incomplete`.

All 3,660 unique events were accepted, processed, and present in simulator
receipts; unaccounted events and duplicate business effects were zero. Stable
drain and reconciliation passed, CloudWatch had complete required buckets, and
pre-cleanup diagnostics showed the original simulator still running and healthy.
Per-step ingestion p95 was approximately 59–80 ms with zero HTTP request errors.
These are useful observations, not a qualified control or elasticity result.

The existing session controller completed unconditional cleanup without
intervention: `teardown_verified` at 2026-09-03 14:12:34 UTC, all 30 native
inventory categories zero, no cleanup errors, and the fixed failure retained
in the phase journal. Stage 9.7 never started. Historical artifacts remain
unchanged; this implementation does not salvage or relabel the failed result.

The correction bounds each step independently before indexing or sending HTTP.
It records a no-request closing iteration in `driver_boundary_iterations`,
permits at most one per step, and fails larger overruns via `driver_errors`.
Full configured step durations replace the insufficient one-millisecond timing
adjustment. The 1/5/10/25/10/5/1 rates, 660-second waveform, 3,660-event manifest,
and all HTTP-count, 201-response, SLO, drop, drain and reconciliation gates remain
unchanged. Malformed or overlapping input slices fail initialization.

`make elasticity-driver-check` now executes the real driver module with mocked
k6 APIs, including all boundary cases and the failed dispatch sequence, before
checking the pinned k6 image. The previous source-text assertion could not
detect the bug. `make elasticity-driver-http-check` additionally exercises real
k6 scheduling against a local deduplicating receiver for the complete waveform;
its artifacts are explicitly local-only and never qualify a cloud experiment.

Local validation of the correction completed with 585 default Python tests,
10 executable JavaScript tests, the pinned k6 inspection, and the 56-test
offline session rehearsal passing; lint and whitespace checks were clean.
The JavaScript suite rejects the previous driver (nine failing tests), including
the cross-slice and recovery-boundary cases. Database integration tests were not
run (15 deselected).

The complete real-k6 local replay passed with 3,660 HTTP requests, 3,660 unique
events, exact counts in every step, zero dropped iterations, zero driver errors,
zero failed acceptance checks, and exit zero. k6 dispatched 3,665 iterations:
five closing-boundary iterations were recorded and safely sent no HTTP request.
The driver SHA-256 matches the saved replay evidence. This validates the guard
with real scheduling as well as forced unit cases, not AWS qualification.
An initial local receiver using HTTP/1.0 hit connection timeouts and was stopped;
its failed evidence was retained. The successful helper uses keep-alive and a
larger listen backlog. Both disposable local containers were removed.

Local artifacts (not cloud-session evidence) are retained under:

```text
results/local-elasticity-driver/
  boundary-fix-20260903T141600Z/           # stopped receiver test; failed
  boundary-fix-keepalive-20260903T142000Z/ # complete 11-minute replay; passed
```
