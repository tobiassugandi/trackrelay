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
