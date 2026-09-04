# Stage 9.6/9.7 AWS elasticity experiment

The causal comparison keeps the asynchronous application, API capacity, RDS,
SQS queues, worker task definition, simulator, benchmark location, and workload
unchanged. The fixed control runs exactly one worker with autoscaling absent.
After a verified application-state reset, the elastic treatment replays the
same workload with a bounded worker autoscaling policy. That policy is the only
treatment variable.

The intended result is not that asynchronous ingestion responds quickly while
work accumulates. A successful treatment must show this complete sequence:

```text
offered load rises
        |
        v
high-resolution accepted arrival demand crosses the frozen threshold
        |
        v
ECS increases worker tasks
        |
        v
completion rate rises and backlog drains
        |
        v
offered load returns low and workers return to one
```

Both treatments must preserve the ingestion p95 below 500 ms, request errors
below 1%, complete request scheduling, every accepted event, zero duplicate
business effects, correct final shipment states, an empty DLQ, and the frozen
drain contract. API latency alone cannot establish sustainable end-to-end load.

## Demo workload v5

The authoritative prospective rules are in the
[v5 workload and measurement contract](aws-elasticity-v5-contract.md).
`aws-elasticity-demo-v5` schedules **5,730 unique events over 630 seconds**:
rates `1/5/10/25/10/5/1` for `30/30/30/180/30/30/300` seconds.
It shortens duration, not peak demand. Both paired treatments use this definition.

The client ingestion gate is each phase p95 <500 ms and errors <1%, checked from
k6 summaries and complete raw per-request records. Ten-second displays include
sample counts. Native minute ALB p95 remains visible corroboration; historical
v2/v3/v4 keep their frozen native-latency gate. No earlier failure is reclassified.
The five-minute recovery must contain at least 60 observed seconds at one worker,
and the peak must contain at least 60 observed seconds at eight workers.

The controller waits until the next UTC minute boundary when necessary and
launches within a one-second tolerance. With the driver image prevalidated and
already present, this minimizes edge-bucket ambiguity and retains a reproducible offset from native boundaries. Thirty-second transitions
can share a native bucket; native points must not be assigned to a single phase.
The saved definition remains authoritative for offered load; CloudWatch points
retain their actual UTC bucket timestamps rather than being relabelled as
single-step measurements.

These rates are a **qualification candidate**, not a frozen capacity result.
Cloud session 3 showed that a 100-event finite batch required about 33 seconds
to reach drained state with one worker, so 5–25 events/s is a reasonable small
search region. The fixed run must still prove that the candidate peak exceeds
one-worker delivery capacity while both API tasks remain healthy, API and
simulator CPU and memory stay below 70%, and RDS plus database-pool evidence
retains headroom. If those conditions do not isolate the worker, revise and
re-freeze the candidate before enabling autoscaling; never tune the workload
after seeing the elastic result.

Candidate v3 follows the completed candidate-v2 negative result. V2 proved
correct delivery and a one-to-eight-to-one worker transition, but its
backlog-triggered scale-out arrived too late to keep maximum outstanding work
under 1,500, and its empty-queue scale-in occurred only after load ended. V3
retains the rates, deterministic event shape, resource bounds, and acceptance
limits while lengthening the demand plateaus and replacing the worker policy.
Historical v2 evidence remains readable and is never relabelled as v3.

The non-worker qualification limits are frozen before session 4: maximum API,
simulator, RDS CPU, API memory, simulator memory, and API database-pool use must
remain below 70%; RDS connections must remain below 50; minimum RDS freeable
memory must remain above 128 MiB; and average RDS read and write latency must
remain below 20 ms in every native window. RDS IOPS and worker CPU are retained
as explanatory evidence rather than capped—the fixed worker is intentionally
the tier expected to become busy.

## Local preparation and driver validation

Prepare an immutable manifest and matching definition without sending traffic:

```shell
make elasticity-prepare \
  ELASTICITY_TREATMENT=fixed \
  ELASTICITY_OUTPUT=results/elasticity-workload/fixed
```

The command refuses to overwrite an existing directory. It writes:

```text
input-manifest.json
workload-definition.json
k6-command.json
```

Validate the complete definition/manifest/driver boundary using the pinned k6
container:

```shell
make elasticity-driver-check
```

The k6 driver creates one non-overlapping constant-arrival-rate scenario per
step, preallocates its bounded VU count, maps the scenario to an exact manifest
slice, tags metrics with the step and authoritative offered rate, applies the
500 ms and 1% ingestion thresholds per step, and fails on dropped iterations.
The fixed and elastic treatments use different run IDs but the same definition,
event shape, random seed, schedule, and thresholds.

## Fixed-control controller

The guarded fixed controller now joins the deployed session-4 stack to the
candidate driver. It accepts only an approved `cloud-session-4-*` manifest in
`async_deployed` state, revalidates the clean approved revision, exact fixed
capacity for all three ECS services, API endpoint, and both queue endpoints,
then runs:

```shell
make aws-fixed-control \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

The controller registers the exact manifest, launches the shell-free pinned k6
command, and samples the application database/outbox summary, source queue,
DLQ, fixed ECS worker counts, and API database-pool snapshot every ten seconds.
It records observation gaps without losing the rest of the timeline, requires
a 180-second continuously drained window, completes reconciliation, then waits
boundedly for every overlapping native CloudWatch minute bucket. Sparse ALB
error and SQS zero values are filled explicitly; utilization, latency, task,
and RDS series must be genuinely published. It writes these files beneath the
session directory:

```text
elasticity/fixed/workload-definition.json
elasticity/fixed/input-manifest.json
elasticity/fixed/k6-summary.json
elasticity/fixed/k6.log
elasticity/fixed/observations.json
elasticity/fixed/observation-failures.json
elasticity/fixed/result.json
elasticity/fixed/cloudwatch.json
elasticity/fixed/summary.json
```

The qualification decision requires complete measurement and native evidence,
worker pressure during the unique 25 events/s peak, one worker throughout,
complete native request scheduling, the ingestion SLOs, an empty DLQ, one API
pool observation for every workload step, and every frozen non-worker headroom
limit. A passing run changes the status to `fixed_control_qualified` and leaves
the stack running for reset and elastic replay. A rejected candidate retains
its evidence, then destroys and verifies the session; it cannot proceed to the
elastic treatment. An unexpected workflow error or interrupt likewise stops
the local k6 process and reaches unconditional teardown.

## Between-treatment reset controller

Only a manifest in `fixed_control_qualified` state can enter the reset. The
controller revalidates the approved clean revision, all fixed ECS capacities,
the API and queue endpoints, a stable one-of-one worker service, and the
absence of an ECS scalable target. It also requires the live application state
to describe exactly the qualified fixed run: one test-run row, all 5,730
events, shipments, durable outbox entries, at least one delivery attempt per
event, 5,730 simulator receipts, a healthy simulator, and empty source and
dead-letter queues.

For an authorized live session, run:

```shell
make aws-experiment-reset \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

The application clears only the sole exact synthetic run from `test_runs`,
`shipments`, `events`, `delivery_attempts`, and `delivery_outbox`; reusable
partner configuration remains. It clears simulator receipts, purges both SQS
queues, waits the mandatory 60 seconds for SQS purge propagation, then requires
application state, both queues, and the fixed worker service to remain empty
and stable for 30 seconds. Autoscaling must still be absent after verification.
The controller writes:

```text
elasticity/reset/pre-reset-observation.json
elasticity/reset/application-reset.json
elasticity/reset/observations.json
elasticity/reset/result.json
```

Success changes the session status to `experiment_reset_verified` and leaves
the unchanged stack running for the elastic treatment. Any refusal, HTTP/AWS
failure, timeout, or interrupt after the reset workflow begins triggers full
session teardown and native teardown verification. Do not retry a failed reset
against a partially changed stack.

## Worker-autoscaling transition

The elastic treatment uses frozen policy v5. Two API replicas publish global
accepted arrivals and unfinished/completed events every ten seconds. Maximum
`ArrivalRate` >=3/s for two ten-second buckets requests exactly eight workers.
Maximum avoids double-counting shared database snapshots.

For contraction, each publisher tracks a fresh `QuietSeconds` timer: rate <2/s,
unfinished events <10, and the sum of all three SQS work counts <10. Busy or
failed samples, restarts and gaps over 15 seconds reset it. A ten-second Minimum
timer >=180 seconds permits return to one. Missing timer data is filled with
zero, which cannot authorize contraction. Both directions retain 60-second
cooldowns. This replaces the stale native-minute scale-in signal; it is not
scheduled scaling or laptop-driven capacity control.

Both treatments include identical telemetry, bounded database pools and AWS
timeouts, queue-scoped attribute permission, and structured request/query timings.
The transition still creates only the scalable target, two policies and two
alarms. Other capacity, images and definitions remain unchanged. The maximum adds
seven 0.25-vCPU/0.5-GiB workers. Native evidence retains its real resolution.
See the v5 contract for latency population, raw timing retention, conservative
completion-window rules, and the new expanded-peak observation gate.

After the reset succeeds, run:

```shell
make aws-elasticity-transition \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

The controller accepts only `experiment_reset_verified`, revalidates the clean
approved revision, immutable image digests, exact fixed service configuration,
empty application/simulator/queue state, and a one-of-one worker. It saves a
Terraform plan and rejects it unless its only meaningful resource actions are
creation of the one target, two policies, and two alarms. It hashes and applies
that exact saved plan, then verifies the typed Terraform output, native target
bounds and suspension state, both exact-capacity policies, alarm-to-policy
wiring, and unchanged empty one-worker state. Evidence is written under:

```text
elasticity/transition/pre-apply.json
elasticity/transition/terraform-autoscaling.tfplan
elasticity/transition/terraform-plan.log
elasticity/transition/plan-evidence.json
elasticity/transition/terraform-apply.log
elasticity/transition/evidence.json
```

Success advances the manifest to `worker_autoscaling_verified` and leaves the
stack ready for the identical elastic replay. A plan mismatch, apply failure,
verification timeout, HTTP/AWS failure, or interrupt destroys and natively
verifies the complete session. Native teardown includes the target, policies,
and alarms even if Terraform state is unexpectedly incomplete.

## Elastic-treatment controller and final teardown

The locally tested elastic controller accepts only `worker_autoscaling_verified`.
It reads and re-evaluates the saved fixed qualification, verifies that the reset
and transition reference that exact fixed run, and reuses its unchanged workload
definition. The shared engine registers a new run ID, uses the same driver and
ten-second observations, and applies the same 1,200-second post-load drain
deadline, 180-second stable-empty window, and reconciliation. A driver that
exceeds the scheduled duration by 120 seconds is stopped and triggers cleanup
in either treatment.

Before sending traffic, the controller requires empty application and queue
state at one worker, unchanged approved Git revision and image digests, stable
API/simulator capacities, and the exact native autoscaling policy. A read-only
Terraform plan must report no changes; the controller never applies that plan.
It repeats the configuration, non-worker capacity, and policy checks after
collection. Any mismatch invalidates the run and triggers cleanup.

For an explicitly authorized future session, after the transition succeeds:

```shell
make aws-elastic-treatment \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

The pre-data elastic contract adds these explicit qualification bounds:

- Outstanding accepted events, sampled queue work, and the conservative sum of
  native visible/in-flight/delayed maxima may not exceed 1,500. This is one
  minute of offered peak traffic, not a capacity inferred from measured data.
- Native oldest-message age may not exceed 180 seconds. This bounds time spent
  waiting in SQS; final reconciliation and the unchanged drain deadline still
  cover completion outside SQS, including unpublished outbox work.
- Desired and running workers must remain within 1–8. Both live samples and
  native metrics must show expansion before recovery; a desired count of eight
  without actual running workers does not pass.
- During the five-minute low-rate recovery, the sampled one-worker suffix must
  last at least 60 seconds and reach within 30 seconds of the waveform's end.
  The last complete native recovery minute must also show one running worker.
  Returning to one only after traffic stops does not pass.
- Observation gaps above 30 seconds, missing API-pool coverage, any observed
  DLQ messages, incomplete scheduling, correctness failures, or any shared
  ingestion/non-worker headroom violation reject the treatment.

These are experiment acceptance criteria, not measured production limits. They
are saved before traffic in `elasticity/elastic/contract.json`; they do not
change the workload, ingestion SLOs, or drain rules between treatments.

The controller retains the same workload, manifest, driver log, observations,
result, native metrics, and summary filenames as the fixed controller beneath
`elasticity/elastic/`. It also saves `pre-load.json`, before/after read-only
plans and hashes, plan logs, and native policy verification. `summary.json`
contains the contract, policy, exact fixed-run link, shared guardrail decision,
and elastic qualification. Machine-loaded result/summary JSON excludes computed
fields and recomputes them on read; the driver definition still includes its
required computed counts.

**This command tears the stack down even on success.** It secures the fixed and
elastic evidence first, records `elastic_treatment_qualified` or
`elastic_treatment_rejected`, then destroys the session and natively verifies
teardown. Final lifecycle status is `teardown_verified`; treatment outcome
remains in `elastic_treatment.qualified` and the retained summary. Failure or
interrupt also attempts both destroy and verification, preserving cleanup errors
alongside the original failure. No cloud experiment has yet been executed.

## Offline comparison and report

`make aws-elasticity-report` reads the local session evidence after teardown. It
does not invoke AWS, Terraform, Git, Docker, or the API, require credentials, or
change the session journal. For a completed, explicitly authorized session:

```shell
make aws-elasticity-report SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

It writes a new `elasticity/report/` directory beneath the session with:

```text
comparison-report.json
comparison-report.md
comparison.png
comparison.svg
```

The large figure uses matching axes for fixed and elastic treatments across
offered load, observed running workers (with native minute averages), SQS work
and outstanding accepted events, and native ingestion p95 with its 500 ms SLO.
Recovery and post-load drain windows are shaded. Native timestamps retain their
actual minute boundaries; observation gaps are not interpolated. Step-level
driver p95 remains separate from native p95 and is never averaged into a new
percentile.

Before rendering, the loader requires `teardown_verified`, a complete zero
native-resource inventory (including autoscaling), empty Terraform state, the
matching qualified fixed run, verified reset and transition, unchanged revision
across the phase journal, before/after native policy and no-change plan evidence,
and an empty one-worker pre-load state. It re-evaluates the treatment decisions,
checks ordering and counter consistency, and hashes its source files. A valid
rejected elastic treatment produces a negative report; missing or contradictory
provenance refuses publication. Output is staged before publication, and an
existing report directory is never overwritten. To regenerate after a local
reporting change, select a fresh `ELASTICITY_REPORT_OUTPUT` directory.

### Frozen short-step support method v2 (workload-v5 window amendment)

Passing the elastic waveform and demonstrating a higher supported rate are
separate claims. A low ingestion latency by itself establishes neither. The
report evaluates both treatments using the same pre-data method:

- Require the common ingestion, correctness, non-worker headroom, and complete
  observation gates for the whole run. Re-establish the 180-second stable drain
  from actual post-load samples, within the unchanged 1,200-second deadline.
- For each plateau, use the first and last actual samples inside its scheduled
  window. Each edge must be covered within 30 seconds, gaps must not exceed 30
  seconds, and for v5 the span must be at least the greater of 10 seconds and
  plateau duration minus 60 seconds. Historical profiles keep the 30-second
  minimum. Retain the exact sampled interval in the report.
- Require completed-event throughput at least equal to the offered rate over
  that interval and non-growing outstanding accepted events. Apply the same
  1,500-event/message bound and 180-second native oldest-message-age bound to
  both treatments, including all overlapping native buckets conservatively.
- Require every occurrence of a rate to pass; do not select only its favorable
  rising or falling plateau. Report the highest such observed rate in each
  treatment. Missing coverage means **not established**, not zero capacity.
- Emit the observed-step rate ratio only when the full comparison is qualified
  and both supported rates exist. A missing return to one worker, for example,
  prevents an elasticity claim even if a high-rate step passed.

These short, ordered plateaus are not independent steady-state capacity tests.
The resulting ratio is an **observed supported-step multiplier**, not an estimate
of maximum production capacity. Cumulative completion counters may include work
from an earlier plateau; throughput plus non-growing backlog measures observed
processing support, not per-event end-to-end latency. Scale-out, return-to-one,
and drain times are first observed samples, not exact transition timestamps.
These conservative reporting rules are frozen before cloud session 4 and do not
change the workload or tune the scaling intervention after seeing its result.

This paired fixed-versus-elastic async run answers the elasticity question:
whether cloud workers are acquired for high demand and released after demand
falls while end-to-end guardrails continue to pass. Its 1-to-25 offered-load
swing is not the claimed async-versus-synchronous traffic multiplier. That
headline requires a separate, pre-frozen capacity staircase comparing the
synchronous reference and modernized asynchronous architecture with matched
correctness, delivery completion, observation duration, and stop rules. Do not
add a 50 events/s step to candidate v3 after seeing its result or substitute API
acceptance latency for completed delivery throughput.

Local synthetic reporting tests (no cloud resources):

```shell
uv run --locked pytest tests/test_aws_elasticity_report.py
```

No synthetic test result is a cloud measurement. The real README headline remains
unpublished until the approved session has produced qualifying evidence.

## Full-session orchestration and local rehearsal

The [session-4 operator runbook](aws-elasticity-runbook.md) documents local
preflight, the full staged resource envelope, explicit approval, execution,
negative results, and recovery. `make aws-elasticity-session` starts from the
fresh reviewed foundation plan and owns apply, deployment, fixed control,
reset, scaling transition, elastic replay, teardown verification, and offline
reporting. Do not run `aws-up` before this combined command.

`make elasticity-session-check` rehearses real controllers and their on-disk
handoffs with simulated external work. It verifies phase ordering, failure and
interrupt cleanup, qualification rejection, independent destroy/verification
failures, and offline report retries. The final journal is written before the
report hashes its inputs. These tests do not provision AWS or establish an
elasticity result.

## Still required before cloud session 4

Review current regional pricing and monthly-budget headroom against the full
resource list, including eight workers and provisioning/teardown time. Agree
the expected operating window and ceiling, then obtain explicit session-4
approval. Local workflow readiness is not authorization to spend or a completed
Stage 9.6/9.7 cloud measurement.
