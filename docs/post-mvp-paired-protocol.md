# Post-MVP fixed-versus-elastic protocol

Protocol revision 1, frozen locally on 2026-09-09 for
[Milestone 1](implementation-plan-2_post-mvp.md). This specifies the next paired
study; it is not a session plan, cloud authorization, or measured result.

## Question and scope

Does enabling worker autoscaling change which tested demand steps the same
async TrackRelay deployment supports while preserving its delivery and
correctness requirements, and does worker capacity return to minimum afterward?

Use one ordered pair per approved attempt: fixed first, then elastic. The order
allows the controller to establish a valid control before changing policy.
It does not control for every time/order effect or establish repeatability.
Report the result as one paired observation. No automatic retry, best-of-many
selection, or within-session tuning is permitted. A repeat needs a separately
reviewed attempt, and all attempts remain in the evidence history.

This study compares worker-capacity policies, not synchronous versus async
architecture, equal-cost efficiency, or maximum sustainable production capacity.

## Frozen treatment definition

Use the [v6 workload and current policy-v6 amendment](aws-elasticity-v6-contract.md),
the inherited [v5 measurement rules](aws-elasticity-v5-contract.md), and the
[post-MVP reporting contract](post-mvp-reporting-contract.md). Historical
versions retain their original rules. Reporting uses
`short-plateau-completion-v4` and `observed-timings-v1`.

| Phase | Offered events/s | Duration | Scheduled start |
| --- | ---: | ---: | ---: |
| Baseline | 1 | 30 s | 0 s |
| Rise | 5 | 30 s | 30 s |
| Rise | 10 | 30 s | 60 s |
| Peak | 25 | 180 s | 90 s |
| Fall | 10 | 30 s | 270 s |
| Fall | 5 | 30 s | 300 s |
| Recovery | 1 | 300 s | 330 s |

Each treatment schedules 5,730 distinct events over 630 seconds. Both use the
same seed (20260901), payload semantics, driver image, five preallocated VUs per
offered event/s, and exact scheduling checks; only run namespaces differ.
Load starts at the controller's UTC-minute boundary with its one-second launch
tolerance. Do not add warm-up traffic or extend a favorable plateau.

Keep the application revision/images, API capacity (two tasks), worker task
size, simulator capacity/healthy behavior, RDS, SQS, network placement, driver
machine, and collection configuration unchanged. Record exact task sizes,
database settings, image identities, and infrastructure plan in the session
evidence; this document does not substitute for those deployment identities.

Fixed treatment: exactly one worker, no worker autoscaling target. Elastic
treatment: start empty at one worker, then let policy v6 request eight after
two ten-second arrival-rate buckets ≥3/s. Scale-in requires 120 continuously
quiet seconds under the existing demand/work conditions; cooldowns are 60
seconds. Detection, startup, and shutdown add delay. Never manually change
workers during either workload.

## Execution order and stopping rules

1. Complete local preflight and commit a clean revision. Prepare a fresh saved
   foundation plan and full staged resource/cost review. Obtain explicit
   session approval before the combined controller provisions anything.
2. Provision once and deploy the fixed topology. Run fixed measurement and
   qualification. If qualification fails, retain evidence and tear down;
   do not continue to elastic or resume the attempt later.
3. For a qualified control, reset its exact synthetic application data,
   simulator receipts, and queues using the existing guarded reset. Retain
   purge propagation and stable-empty proof. Do not redeploy or resize services.
4. Apply only the existing guarded five-resource worker-autoscaling transition.
   Verify unchanged environment and empty-at-minimum state, then replay the
   identical workload. Retain elastic evidence whether it qualifies or fails.
5. Destroy the complete stack, verify Terraform state and all 30 native
   inventory categories, and finalize the journal before offline reporting.
   Preserve independent workflow, cleanup, and reporting failures.

Use the [combined-session runbook](aws-elasticity-runbook.md), not concurrent
manual controllers. Partial runs are not resumable. A report failure after
verified teardown can be retried locally into a fresh output directory.

## Three distinct decisions

| Decision | Requirements and consequence |
| --- | --- |
| Valid fixed control | Complete scheduling/measurement, ingestion gates, exact delivery/reconciliation, stable drain, non-worker headroom, empty DLQ, one worker throughout, and observed peak queue pressure. Only this permits the transition. Queue pressure alone does not prove overload. |
| Supported demand step | Shared treatment gates plus complete sampled/native windows, completed throughput ≥ offered rate, non-growing outstanding work, backlog ≤1,500, and oldest native age ≤180 seconds. Both treatments use the same step rules. Every occurrence of a rate must pass. |
| Qualified elastic demonstration | Shared gates plus existing whole-treatment backlog/age limits, sustained eight-worker peak evidence for at least 60 seconds, and at least 60 seconds of observed recovery at one worker with v6 native corroboration. |

A valid fixed control may fail individual support steps because workers cannot
keep up, while ultimately draining and reconciling within the declared deadline.
That is useful comparison evidence. Missing observations, ingestion failure,
non-worker saturation, failed reconciliation, or a missed drain contract are
not worker-only capacity findings and cannot be waived to obtain a pair.

Ingestion gates are full-phase p95 <500 ms and request errors <1%, with exact
request counts, zero drops, and complete raw timing records. Ten-second p95
displays may cross 500 ms without redefining the phase gate; retain those spikes.
Native ALB p95 is corroboration under v6, not a substitute population.

Retain the existing strict headroom limits from the workload definition:
API/simulator CPU and memory, API pool use, and RDS CPU below 70%; RDS connections
below 50, freeable memory above 128 MiB, and read/write latency below 20 ms.
Reconciliation requires every scheduled event accepted and processed, no failed,
pending, unaccounted, duplicate business effects, or incorrect final states,
and exact unique simulator receipts.

Post-load drain requires 180 seconds of stable emptiness within the existing
1,200-second deadline after load ends. Report validation requires observation
coverage with no gap above 30 seconds. V6 step completion uses actual sampled
windows of at least max(10 seconds, step duration minus 60 seconds), with boundary
coverage within 30 seconds. No post-load completion can rescue a failing step.

## Reporting and claim boundaries

Use the consecutive-rate rule and timing origins in the
[reporting contract](post-mvp-reporting-contract.md). The first unsupported
distinct rate ends the reported envelope; retain all higher-rate diagnostics.
No supported baseline means no rate or multiplier. A ratio ≤1 is a valid
outcome, not a reason to retune a completed attempt.

Publish the aligned fixed/elastic figure, per-step completion table, rejection
reasons, scale-out/return observations, peak-clearance diagnostics, and post-load
drain times. Keep desired/running service observations distinct from exact task
transitions or billing. Peak clearance is a diagnostic, not a new pass gate.

If both treatments are valid and elastic supports a higher step, state the
highest consecutive passing tested rates and their ratio under this waveform.
If only expansion/contraction is established, state that without claiming a
capacity improvement. Rejected/incomplete evidence cannot earn a positive
headline. No result here measures per-event downstream latency percentiles;
that remains instrumentation work for the matched architecture study.

## Readiness remaining before a cloud proposal

The audit and reporting changes have focused local coverage; this protocol
review does not claim a fresh complete preflight. Next:

- Run the runbook's local application, infrastructure, image, and driver checks
  against the intended revision; retain outcomes and resolve failures locally.
- Recheck operator guidance and session configuration against that revision.
- Prepare a unique session identity, region, exact staged resource list, current
  regional price estimate, expected total duration, cost ceiling, monthly-budget
  headroom, and cleanup/recovery plan. Historical dollar ceilings and approvals
  do not carry over to the new attempt.
- Present the complete reviewable proposal for explicit approval. This protocol
  authorizes neither provisioning nor a retry.
