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

## Third attempt: qualified control, reset contract failure

Session: `cloud-session-4-20260903T144402Z`, measured revision `0d61173ed2aa`.

The fixed control qualified: k6 exited zero with no dropped iterations, all
3,660 unique events were accepted and delivered, stable drain and reconciliation
passed, and all native headroom/ingestion gates passed. The retained fixed
summary has `qualified: true` and no rejection reasons. This control remains
useful evidence, but it is not a paired elasticity result.

Reset failed immediately after entering phase 4. No pre-reset observation was
saved. The controller reads `/api/v1/experiments/state` before any reset POST or
queue purge, and the API reads the real simulator's status at that point. The
simulator returns `HEALTHY`; reset's separate string contract expected `healthy`
and rejected it. A local reproduction connecting both real ASGI apps returned
HTTP 503, matching this failure path. The retained cloud exception recorded only
`HTTPStatusError`, not its exact response status/body. A second latent mismatch
was reproduced locally: reset's lowercase mode PUT returned HTTP 422 because
the simulator accepts uppercase enum values.

The controller stopped before the worker-only scaling transition. Its existing
unconditional cleanup completed without intervention: `teardown_verified` at
2026-09-03 15:22:02 UTC, all 30 native inventory categories zero, no cleanup errors.
The original journal and all cloud measurements remain unchanged.

The correction reuses `SimulatorMode` for reads, snapshot serialization,
healthy-state checks, and the PUT payload. This also restores the omitted
`TIMEOUT` mode to the state contract; degraded modes still prohibit reset.
Unknown or lowercase wire values are rejected rather than silently normalized.
Reset HTTP failures now become safe controller errors containing the fixed
GET/POST operation and status code, or the transport exception type. Existing
progress and journal handling retain that description without exposing hosts,
credentials, headers, response bodies or arbitrary exception text. There are no
new HTTP retries, particularly for potentially partially committed reset POSTs.

The old reset service mocks invented lowercase simulator responses. New tests
connect the real controller, API and simulator handlers/response models, with
only the external AWS observations and clock simulated. They check full reset,
stable-empty proof, partner preservation, all five modes, refusal of degraded
or mismatched runs without writes, and JSON evidence round-trips. A separate
isolated PostgreSQL 17 run covers the actual `TRUNCATE` branch; SQLite alone
would exercise only the `DELETE` fallback. Error tests cover HTTP 409/422/503
and GET/POST timeouts, safe diagnostics, one cleanup, and no retry or queue purge
after HTTP failure.

Local validation passed: 607 default Python tests, the separately selected
PostgreSQL 17 contract test, and all three rebuilt API/worker/simulator image
smoke tests. Lint and whitespace checks were clean. The new real-app state test
first reproduced HTTP 503 on the old implementation and passed after correction.
The dedicated PostgreSQL container and its temporary database were removed;
no existing development database or AWS resource was used. The other 15
database integration tests were not run for this correction.

This correction is locally verified, not another cloud run. A fresh reviewed
plan, new session ID, new immutable images, and explicit spending/teardown
approval are still required. Do not combine this control with an elastic run
from a different session or reconstruct a successful reset after teardown.

## Fourth attempt: successful reset, non-idempotent Cloud Map plan

Session: `cloud-session-4-20260903T153822Z`, measured revision `28117b7db199`.

The fixed control qualified with complete measurement and observed worker
pressure. The corrected reset then completed at 2026-09-03 16:08:24 UTC. This
confirms the simulator-mode correction in the real deployment, but the retained
control is still not a paired elasticity result.

Before applying the worker policy, the transition controller inspected its saved
Terraform plan and rejected it. In addition to the five expected autoscaling
creates, the plan proposed replacing
`aws_service_discovery_service.simulator[0]` and updating
`aws_ecs_service.async_simulator[0]`. The saved plan showed the precise cause:
the configured empty `health_check_custom_config {}` was absent from refreshed
state, so adding it was a replacement-only difference whose new discovery ARN
would flow into the ECS service registration. Terraform reported six creates,
one update and one destroy. The strict transition guard stopped before apply;
no autoscaling resource or elastic workload was started.

This is a known AWS provider 6.x behavior: an empty custom-health map expands to
no API configuration and is not retained on refresh, while the block itself is
replacement-only. Setting its deprecated `failure_threshold = 1` would make the
block concrete, but would introduce a configuration change and repeated provider
warnings. Removing the ineffective empty block instead matches the resource that
AWS had already created and keeps the treatment boundary unchanged. The
Terraform contract now explicitly requires no custom-health block. Plan rejection
still requires exactly the same five autoscaling creates, but its error identifies
unexpected addresses and actions without emitting before/after values.

The combined controller performed unconditional cleanup without intervention.
The session reached `teardown_verified` at 2026-09-03 16:18:33 UTC; all 30 native
inventory categories were zero, with no cleanup or diagnostic errors. Original
plans, measurements and phase journal remain unchanged.

Local validation covers the two-change Cloud Map/ECS plan shape, non-disclosure
of plan values in the error, and the absent custom-health Terraform contract.
This correction does not authorize another AWS attempt. A fresh reviewed plan,
new session ID, matching clean revision, and explicit spending and unconditional
teardown approval remain required for another paired run.

## Fifth attempt: transient observation timeout

Session `cloud-session-4-20260903T170106Z` reached the elastic treatment on
revision `68e2c8a4467b`, but a bounded application observation timed out after
the workload ended. Its evidence is retained as an interrupted candidate-v2
attempt, not a capacity result. The combined controller completed unconditional
cleanup: `teardown_verified` at 2026-09-03 18:19:51 UTC, all native inventory
categories zero, and no cleanup or diagnostic errors.

## Sixth attempt: completed candidate-v2 negative result

Session `cloud-session-4-20260903T182124Z` completed both candidate-v2
treatments and exact reconciliation. Fixed and elastic runs each accepted and
delivered all 3,660 events with no duplicate business effects and an empty DLQ.
The elastic worker pool changed from one to eight tasks and eventually returned
to one; native ingestion and non-worker headroom checks passed.

The frozen v2 acceptance gate nevertheless rejected the elastic treatment for
`worker_recovery_not_observed_during_load` and `backlog_bound_exceeded`.
Maximum observed outstanding work was 1,577 against the frozen 1,500 bound.
The desired count first reached eight at about 292 seconds and running count at
about 325 seconds, after the short peak had already created most of the backlog.
Desired count returned to one at about 614 seconds and running count at about
626 seconds, too late to establish a full one-worker recovery interval before
the 660-second workload ended. These are policy/workload-timing failures, not
delivery correctness failures.

The offline negative comparison is retained under that session's
`elasticity/report/` directory and correctly publishes no elasticity multiplier.
The controller reached `teardown_verified` at 2026-09-03 19:12:34 UTC, with all
30 native inventory categories zero and no cleanup or diagnostic errors. The
measurement remains immutable and readable as candidate v2.

Candidate v3 is a newly frozen experiment, not a relabelling or repair of v2.
It retains the 1/5/10/25/10/5/1 rate shape and all acceptance bounds, lengthens
the plateaus to 60/120/120/300/60/60/420 seconds, scales out from one native
minute with at least 300 SQS sends, and scales in only after three minutes with
both fewer than 120 sends and fewer than ten visible, in-flight, and delayed
messages. A future run requires a fresh session ID, plan review, and explicit
cost and teardown authorization.

## Seventh attempt: candidate-v3 reconciliation timeout

Session `cloud-session-4-20260903T211203Z` ran candidate v3 at revision
`5891efdcbc53`. The fixed workload issued exactly 10,680 HTTP requests with no
dropped iterations or driver errors. The retained observations reached 10,680
persisted, processed, and delivered events, an empty source queue and DLQ, and
one running fixed worker. They also cover more than the required 180-second
stable-empty interval.

The controller then timed out before writing `elasticity/fixed/result.json`.
The final retained observation is at 21:59:31 UTC, ECS diagnostics began at
21:59:46 UTC, and the next operation in the controller is the reconciliation
POST with a 10-second client timeout. The API's final-shipment comparison was
quadratic: for each of 10,680 expected shipments it scanned all 10,680 manifest
events to locate the expected final event, roughly 114 million comparisons.
This scaling defect was not exposed by candidate v2's smaller 3,660-event
manifest. Without a reconciliation report, native CloudWatch summary, or fixed
result, this attempt is incomplete and cannot qualify or be paired with an
elastic treatment.

The correction derives all final events in one manifest pass, retaining the
same latest-timestamp and sequence-number semantics. On the retained 7.6 MB
manifest, local parsing took about 0.033 seconds and final-event selection about
0.001 seconds. The controller now gives only reconciliation a bounded
120-second HTTP budget; registration, observations, and health-sensitive calls
retain their short timeout. Regression coverage requires a single manifest
iteration and verifies the dedicated timeout. Local validation passed all 612
default Python tests and lint; the 16 separately selected database integration
tests were not run for this correction.

Initial cleanup also failed because the AWS login session expired while
Terraform was waiting for ECS service deletion and internet-gateway detach.
After reauthentication, the documented `aws-down` recovery completed and the
independent `aws-verify-down` check passed. The session reached
`teardown_verified` at 2026-09-03 22:41:47 UTC, Terraform state is empty, and
tagged and all native inventory categories are zero. The original workflow and
cleanup errors remain in the journal rather than being rewritten.

This correction is local only and does not salvage the attempt or authorize a
retry. A fresh session ID, reviewed plan, matching immutable images, and
explicit spending and unconditional teardown approval are still required.

## Elastic-only v3 diagnostic: retry accounting, metric gap and late recovery

Session `cloud-session-4-20260904T060729Z` used revision `1885dc2c283e`.
The diagnostic issued exactly 10,680 requests with zero ingestion errors and
all per-step p95 values below 108 ms. All 10,680 unique simulator receipts
existed, no duplicate business effects or wrong final states were reported,
and stable drain completed. Sampled queue work peaked at 1,126 (bound 1,500),
and native oldest-message age peaked at 66 seconds (bound 180).

Three separate issues prevent a pass:

- Reconciliation reported four unaccounted identities. The old comparison
  required receipt count to equal successful HTTP attempt count. A local
  reproduction with two successful attempts and one idempotent receipt produced
  a false mismatch. This is a plausible explanation, not proof of the four
  historical mismatches: that report did not retain their identities or reasons.
  The correction compares successful-delivery presence with one business
  receipt; zero successes/zero receipts remains unfinished, not successful
  delivery. Missing receipts, duplicate effects, unexpected identities and
  content mismatches still fail their existing gates. New reports record
  `idempotent-effects-v2`, retry counts and per-identity evidence without raw
  payloads. Old reports retain their original counts and accounting semantics.
- Native RDS CPU lacked the 06:38 UTC (13:38 Jakarta) bucket, while 06:39 UTC
  existed. Eight attempts did not recover this interior gap. Other required
  series had complete bucket timestamps. The collector now names missing UTC
  buckets, interior gaps, unexpected timestamps and duplicates. It still fails
  closed and does not interpolate or zero-fill resource utilization. More
  precise diagnostics do not guarantee AWS will publish a missing point.
- Desired workers reached eight at 311.8 seconds, running workers reached eight
  at 346.7 seconds, desired count returned to one at 1,073.7 seconds and running
  count at 1,084.8 seconds. The observed one-worker suffix was only 45.15 seconds,
  short of the frozen 60-second gate. Low-demand/low-queue native buckets began
  at 06:32 UTC, but desired capacity changed around 06:37:54 UTC. ECS then took
  about 11 seconds to reach one worker. This points to latency before desired
  capacity changed; without retained alarm/action history the precise cause
  cannot be established.

Candidate v4 adds 120 seconds of recovery to v3: the last step is 540 seconds,
total duration 1,260 seconds, and total requests 10,800. All preceding steps,
policy version 3, three-period scale-in rule, and acceptance limits are
unchanged. Both future paired treatments must use the new definition. This is
observation margin, not a claim that scale-in was made faster or that the old
attempt now passes.

Cleanup reached `teardown_verified` at 2026-09-04 06:51:28 UTC. No historical
artifacts were rewritten and no new AWS run was started for these corrections.
Local validation passed 655 default Python tests (16 integration tests excluded),
lint, 10 executable JavaScript tests and the pinned k6 inspection. A complete
21-minute real-HTTP replay was not run for this change.
