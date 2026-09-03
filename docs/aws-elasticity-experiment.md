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
SQS backlog exposes pending work
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

## Candidate workload v2

`aws-elasticity-candidate-v2` is now defined in code and consumed from a saved
JSON definition by `load/elasticity-steps.js`:

| Step | Offered rate | Duration | Scheduled events |
| --- | ---: | ---: | ---: |
| baseline | 1 event/s | 60 s | 60 |
| rise-5 | 5 events/s | 60 s | 300 |
| rise-10 | 10 events/s | 60 s | 600 |
| peak-25 | 25 events/s | 60 s | 1,500 |
| fall-10 | 10 events/s | 60 s | 600 |
| fall-5 | 5 events/s | 60 s | 300 |
| recovery | 1 event/s | 300 s | 300 |

The complete waveform schedules 3,660 unique `CREATED` events over 660 seconds.
Every plateau aligns to CloudWatch's 60-second native metric period. The final
five-minute low-rate window exists to observe backlog recovery and, in the
elastic treatment, return to the minimum worker count.

The controller waits until the next UTC minute boundary when necessary and
launches within a one-second tolerance. With the driver image prevalidated and
already present, this minimizes edge-bucket ambiguity and keeps the 60-second
transitions consistently offset from native boundaries.
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

Candidate v2 preserves v1's exact waveform and deterministic events. It adds
the fixed, machine-evaluated native and database-pool qualification limits
rather than silently changing the meaning of the earlier saved definition.

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
to describe exactly the qualified fixed run: one test-run row, all 3,660
events, shipments, durable outbox entries, at least one delivery attempt per
event, 3,660 simulator receipts, a healthy simulator, and empty source and
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

The elastic treatment uses one frozen step-scaling policy. The source queue's
native `ApproximateNumberOfMessagesVisible` maximum is evaluated in 60-second
periods. Ten or more visible messages for one period sets the worker service to
eight tasks. Zero visible messages for three consecutive periods returns it to
one. Both directions use exact-capacity adjustments and a 60-second cooldown;
missing data does not cause scale-out and is treated as empty for scale-in.

This is an experiment policy, not a general production recommendation. Its
purpose is to make acquisition and release obvious within the fixed waveform:
one complete backlog bucket can trigger expansion, while the five-minute
recovery step contains the three empty buckets required for contraction. The
maximum adds at most seven 0.25-vCPU/0.5-GiB Fargate workers—1.75 vCPU and
3.5 GiB above the fixed control—only while the alarm-driven service desires
them. It adds one scalable target, two scaling policies, and two CloudWatch
alarms; API, simulator, RDS, SQS, task definitions, images, and the minimum
worker count remain unchanged.

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

## Still required before cloud session 4

- A compact result model and plot generator that evaluate sustainable
  end-to-end load and render the aligned causal comparison.
- Local failure-path tests for plotting and the complete session teardown
  sequence.

No cloud-session-4 plan should be proposed until these pieces are locally
complete and the resource/cost effect of the maximum worker count is reviewed.
