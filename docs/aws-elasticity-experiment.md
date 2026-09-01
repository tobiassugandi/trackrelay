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

## Candidate workload v1

`aws-elasticity-candidate-v1` is now defined in code and consumed from a saved
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

These rates are a **qualification candidate**, not a frozen capacity result.
Cloud session 3 showed that a 100-event finite batch required about 33 seconds
to reach drained state with one worker, so 5–25 events/s is a reasonable small
search region. The fixed run must still prove that the candidate peak exceeds
one-worker delivery capacity while both API tasks remain healthy, API and
simulator CPU and memory stay below 70%, and RDS plus database-pool evidence
retains headroom. If those conditions do not isolate the worker, revise and
re-freeze the candidate before enabling autoscaling; never tune the workload
after seeing the elastic result.

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

## Still required before cloud session 4

- A treatment runner that registers the manifest, executes k6, samples aligned
  database, queue, ECS, ALB, simulator, and RDS evidence, reconciles the run,
  and retains partial evidence on failure.
- A reset controller that proves database experiment rows, outbox entries,
  queues, DLQ, and simulator receipts are empty without recreating the stack.
- A bounded worker autoscaling policy and an exact apply/verification boundary
  between the fixed and elastic treatments.
- A compact result model and plot generator that evaluate sustainable
  end-to-end load and render the aligned causal comparison.
- Local failure-path tests for the runner, reset, metric collection,
  reconciliation, plotting, and unconditional session teardown.

No cloud-session-4 plan should be proposed until these pieces are locally
complete and the resource/cost effect of the maximum worker count is reviewed.
