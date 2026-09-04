# Elastic-only workload-v5 / policy-v5 diagnostic

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

New runs use the [v5 workload and measurement contract](aws-elasticity-v5-contract.md):
**5,730 events over 10½ minutes**, still peaking at 25 events/s. Phase durations
are `30 → 30 → 30 → 180 → 30 → 30 → 300` seconds. Provisioning, migration,
minute alignment, drain and teardown are additional. Drain retains its
180-second stable-empty requirement and 20-minute deadline.

Policy v5 uses ten-second telemetry in both directions: two arrival-rate buckets
at or above 3/s request eight workers; 180 seconds of continuously sampled low
demand and low outstanding/queue work permits return to one. Failures and stale
samples reset that quiet timer. Scale-out and Fargate startup still have latency.
Qualification requires at least 60 observed seconds at eight workers during
peak, plus 60 at one worker during recovery. Backlog and correctness bounds
remain unchanged.

Ingestion p95 <500 ms is checked per phase from k6 and complete raw request
records. Native ALB minute p95 remains visible corroboration, not a v5
ingestion-only gate. Earlier versions retain their original rules. Ten-second
p95 displays include sample counts; they are not averaged into a phase p95.

Use a fresh deployment and rebuilt API image. Telemetry runs in both fixed and
elastic deployments, so the transition still creates only five policy resources.
There are six custom metrics, two high-resolution alarms, bounded database
counting queries, SQS attribute reads and structured timing logs. Review those
costs and overhead before approval. Accepted arrival rate can be suppressed by
an ingestion bottleneck; ingestion and headroom gates remain mandatory.
No local test implies a passing cloud result.

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
gauges and server p95), `alarm-demand-high.json`, `alarm-release-safe.json`, and
`scaling-activities.json` (including non-scaling decisions), endpoint timing logs
and telemetry-query logs. Per-request records are in `k6-points.json` and
`result.json`; ten-second client latency/count displays are in
`request-timing-windows.json`. Inspect
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
