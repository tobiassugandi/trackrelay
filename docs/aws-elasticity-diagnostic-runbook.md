# Elastic-only workload-v4 / policy-v4 diagnostic

Use this workflow while developing autoscaling. It skips the fixed-worker load
and between-treatment reset, but retains the full elastic workload and all
acceptance bounds. It does not establish a capacity multiplier or replace the
final paired experiment. This document is not approval to spend on AWS.

The command owns four phases: foundation apply, async deployment, guarded
worker-autoscaling transition, and elastic measurement. It always attempts
destruction and independent native absence verification after acquiring cleanup
ownership, including on failure or Ctrl-C. Do not run phase commands alongside it.

## Prepare and approve

1. Run the local checks below. Commit intended changes and require clean Git
   status before planning. Do not change code during a run.
2. Follow sections 2 and 3 of the [paired runbook](aws-elasticity-runbook.md):
   authenticate, generate a **new** `cloud-session-4-TIMESTAMP` ID, resolve the
   current API `/32`, save `make aws-plan` with `AWS_DEPLOYMENT_MODE=async`, and
   review the plan and complete staged topology. Retain the exported
   `TRACKRELAY_RUN_SESSION_ID`, `TRACKRELAY_RUN_API_CIDR`, and
   `TRACKRELAY_RUN_SESSION_DIR` variables from those steps.
3. Obtain explicit approval of this **elastic-only diagnostic**, its session ID,
   region, full topology, operating window, cost ceiling, and unconditional
   teardown. Set `TRACKRELAY_RUN_COST_CEILING_USD` and
   `AWS_MONTHLY_BUDGET_USD` to the reviewed numeric amounts. Previous run
   approvals do not authorize a retry.

```shell
make test
make lint
make elasticity-diagnostic-check
```

The topology and maximum worker envelope are unchanged: two API tasks, one
simulator, private RDS, SQS and DLQ, and one to eight workers. Deployment starts
with one worker, but no fixed-worker benchmark is run. The transition proves
the database, simulator and queues are empty, applies exactly the five frozen
policy resources, verifies native policy wiring and emptiness again, and checks
the unchanged environment immediately before load. It never fabricates a
fixed-run ID or reset result.

Candidate v4 sends 10,800 events over 21 minutes at
`1 → 5 → 10 → 25 → 10 → 5 → 1` events/second, using plateaus of
`60 → 120 → 120 → 300 → 60 → 60 → 540` seconds. Allow additional time for
provisioning, images, migration, minute alignment, drain, metric publication and
teardown. Drain still requires 180 continuously empty seconds within a
20-minute post-load deadline. Skipping fixed load saves that treatment's load
and drain plus reset; it does not make the whole session a 21-minute operation.

The workload is unchanged from candidate v4. New deployments use **policy v4**:
each API replica publishes global database-derived `ArrivalRate` and
`OutstandingEvents` gauges to `TrackRelay/Elasticity` every ten seconds, with
one-second storage resolution. The alarm uses **Maximum**, never Sum across
replicas: at least 3 accepted unique events/s for two ten-second periods requests
eight workers. This replaces the native SQS 300-message/minute scale-out signal.
The existing three-minute low-demand/low-queue scale-in rule, cooldowns,
1,500-event backlog bound, 180-second age bound and observed one-worker recovery
gate are unchanged. A ten-second alarm is not a ten-second task-start guarantee.

The publisher is also present in fixed deployments, so the guarded transition
still creates only the five policy resources. It has its own bounded database
pool and CloudWatch timeouts, publishes explicit zeroes on successful empty
samples, logs failures without manufacturing zeroes, and shuts down with the API.
Duplicate retries are not counted as new unique arrivals. This is accepted demand,
not offered traffic: an API/database ingestion bottleneck can suppress this signal.
Existing ingestion and non-worker headroom gates remain mandatory.

Use a fresh deployment and rebuilt API image; do not apply this change to a stack
from an earlier run. Policy v2/v3 evidence stays readable and is not reclassified.
The telemetry adds two custom metrics, a high-resolution alarm, PutMetricData
calls from two API replicas, and one database connection per replica. Review those
costs and overhead before approving another run. No cloud result is implied by
the local implementation. See [AWS high-resolution metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/publishingMetrics.html#high-resolution-metrics).

Review current regional costs and remaining monthly budget before approval.
The ceiling check is not a billing meter or dollar/time kill switch. Do not
infer a lower safe ceiling merely because this workflow is shorter. Keep AWS
authentication valid through cleanup; expired credentials can interrupt destroy.

## Run once

Only after the preparation and explicit approval above:

```shell
make aws-elasticity-diagnostic \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_MONTHLY_BUDGET_USD="$AWS_MONTHLY_BUDGET_USD"
```

The direct CLI is `uv run --locked trackrelay-aws-elasticity-diagnostic` with
the same arguments as the paired session CLI. Use `--help` to inspect them;
`--quiet` suppresses progress, not conclusions, errors or saved evidence.
Normal operation prints timestamped phase, workload, drain, metric and cleanup
progress. Ctrl-C requests cleanup; keep the terminal open while it completes.

## Inspect the outcome

```shell
jq '{status, teardown_verified_at, elasticity_diagnostic_session,
     elastic_diagnostic}' "$TRACKRELAY_RUN_SESSION_DIR/session.json"

jq '{kind, headline_eligible, comparison_multiplier, qualification}' \
  "$TRACKRELAY_RUN_SESSION_DIR/elasticity/diagnostic/elastic/summary.json"

jq 'to_entries | map(select(.value != 0))' \
  "$TRACKRELAY_RUN_SESSION_DIR/aws-native-inventory-after-destroy.json"
```

Success requires `teardown_verified`, diagnostic journal `phase: cloud_complete`,
all qualification gates passing, and zero native leftovers. The summary may be
absent after an interrupted measurement. A retained passing qualification does
not override a subsequent environment-check or teardown failure: inspect both
the journal and top-level teardown status.

Evidence is isolated under `elasticity/diagnostic/transition/` and
`elasticity/diagnostic/elastic/`: exact plans, native policy proof, manifest,
workload definition, k6 output, observations/gaps, reconciliation in `result.json`,
CloudWatch evidence, environment checks and `summary.json`. The summary is
labelled `headline_eligible: false` and `comparison_multiplier: null`.
The paired comparison reporter explicitly refuses a diagnostic session even
if other treatment files are present.

On failure, retain the negative/incomplete evidence and inspect the journal's
`failed_phase`, `workflow_error` and `cleanup_errors`. Reconciliation now records
`accounting_method: idempotent-effects-v2`, successful retry counts, and per-event
`mismatches` with reasons and successful-attempt/receipt counts in `result.json`.
Multiple successful retries may correspond to one idempotently stored business
effect; missing receipts, duplicates and content mismatches still fail.
CloudWatch missing-bucket errors name exact UTC timestamps and identify interior
gaps where later data already exists. Do not zero-fill or interpolate RDS CPU.

Auxiliary timing evidence is collected before native metric qualification under
`diagnostics/scaling/TEST_RUN_ID/`: `high-resolution-metrics.json` (ten-second
Maximum gauges), `alarm-demand-high.json`, `alarm-release-safe.json`, and
`scaling-activities.json` (including non-scaling decisions). Inspect
`collection.json` for collection failures. These are best-effort diagnostics,
not substitutes for complete native metric evidence; raw responses can contain
missing or not-yet-published datapoints. Compare alarm state/action timestamps,
scaling activity timestamps, and the existing desired/running/pending task
observations to separate detection, action, and startup delay. Native bucket
timestamps alone do not reveal when AWS first published the datapoint.

Inspect the journal's
`failed_phase`, `workflow_error` and `cleanup_errors`. If teardown is not
verified, use the paired runbook's `make aws-down` and `make aws-verify-down`
recovery commands with this **same** session ID, `/32`, profile and region.
Reauthenticate first if necessary. Never begin another run while resources
remain. There is no partial-session resume or automatic retry.

Once the diagnostic passes, perform one fresh paired fixed → reset → elastic
session to establish the causal elasticity comparison. The separate matched
synchronous-versus-elastic-async capacity staircase is still required for the
async-versus-sync `X×` headline; neither a diagnostic pass nor the 1-to-25 demand
swing supplies that denominator.
