# Stage 9.5 AWS asynchronous integration runbook

This procedure deploys the asynchronous TrackRelay stack, runs the small
cloud-session-3 integration checkpoint, retains its evidence, and removes every
session resource.

The frozen `1 -> 10 -> 100` values are **total events per test case**, not
events per second. The runner sends each finite batch sequentially and as soon
as the preceding HTTP response permits; it does not schedule a fixed arrival
rate or hold that rate for a duration.

| Case | Meaning | Purpose |
| --- | --- | --- |
| 1 event total | One ingestion request and one asynchronous delivery | Prove the minimum end-to-end path. |
| 10 events total | A small back-to-back batch | Exercise ordering, the durable outbox, queue, worker, and reconciliation with multiple events. |
| 100 events total | A larger back-to-back batch | Make modest backlog, drain, and CloudWatch behavior observable without making a capacity claim. |

The `10, 25, 50, 100, 200 events/s` ladder from Stage 9.3 had a different
purpose: it offered each rate for a fixed ten-second interval to compare
capacity. Reusing that ladder here would turn a correctness checkpoint into a
performance experiment, duplicate later work, and multiply the time spent in
the required 180-second stable-drain window. Rate-controlled stepped load
belongs in the Stage 9.6 fixed-capacity control and the identical Stage 9.7
elastic treatment.

## Operator workflow

The normal workflow has four commands:

```text
make aws-plan
      ↓ explicit approval
make aws-up
      ↓
make aws-async-deploy
      ↓ immediately after successful convergence
make aws-async-integration
```

`aws-up` is the explicit spending boundary. After it succeeds,
`aws-async-deploy` owns image publication, migration, and fixed-service
convergence. A successful deployment deliberately leaves AWS running so
`aws-async-integration` can immediately own all three cases, evidence
collection, unconditional teardown, and native absence verification. Do not
run a second lifecycle, workload, or teardown controller in parallel.

## 1. Create a fresh session identity

Run from the repository root with a clean worktree. Generate a UTC session ID
and derive the public `/32` of the machine that will send the requests:

```shell
export TRACKRELAY_RUN_SESSION_ID="cloud-session-3-$(date -u +%Y%m%dT%H%M%SZ)"

trackrelay_public_ipv4="$(
  curl --fail --silent --show-error https://checkip.amazonaws.com
)"
trackrelay_public_ipv4="$(
  uv run --locked python -c \
    'import ipaddress, sys; print(ipaddress.IPv4Address(sys.argv[1]))' \
    "$trackrelay_public_ipv4"
)"
export TRACKRELAY_RUN_API_CIDR="${trackrelay_public_ipv4}/32"

test ! -e "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID"
printf 'session: %s\ningress: %s\nmode: async\n' \
  "$TRACKRELAY_RUN_SESSION_ID" \
  "$TRACKRELAY_RUN_API_CIDR"
```

Do not choose a cost ceiling until the saved plan, resource list, duration,
and cost estimate have been reviewed.

If an unmodified, approved plan already exists, recover its values instead of
generating a new identity:

```shell
export TRACKRELAY_RUN_SESSION_FILE='results/aws-sessions/APPROVED_SESSION_ID/session.json'
export TRACKRELAY_RUN_SESSION_ID="$(
  jq -er '.session_id' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_API_CIDR="$(
  jq -er '.api_ingress_cidr' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
```

Verify the existing plan before using it:

```shell
test "$(jq -er '.status' "$TRACKRELAY_RUN_SESSION_FILE")" = 'planned'
test "$(jq -er '.deployment_mode' "$TRACKRELAY_RUN_SESSION_FILE")" = 'async'
test "$(jq -er '.git_revision' "$TRACKRELAY_RUN_SESSION_FILE")" = \
  "$(git rev-parse HEAD)"

trackrelay_expected_plan_sha="$(
  jq -er '.plan_sha256' "$TRACKRELAY_RUN_SESSION_FILE"
)"
trackrelay_actual_plan_sha="$(
  shasum -a 256 \
    "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/terraform.tfplan" |
    awk '{print $1}'
)"
test "$trackrelay_actual_plan_sha" = "$trackrelay_expected_plan_sha"
```

Also re-check the current public IPv4. If it differs from the saved `/32`, do
not widen ingress or reuse the plan; create and review a new session instead.

## 2. Run local preflight

Docker must be running. Then execute:

```shell
git status --short
git rev-parse HEAD
make aws-check
make infra-init
make infra-check
make test
make images-smoke
```

Required checkpoints:

- Git status is empty.
- The AWS identity is `trackrelay-admin` in `ap-southeast-3`.
- Terraform formatting, validation, and tests pass.
- The Python test suite passes.
- The API, worker, and simulator production-image smoke checks pass.

The Makefile supplies the AWS profile and region explicitly; no ambient AWS
configuration is required.

## 3. Save and review the plan

Skip this section only when executing an existing plan that passed every check
above.

```shell
make aws-plan \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Inspect the immutable saved plan:

```shell
terraform -chdir=infra/terraform show \
  "../../results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/terraform.tfplan"
```

Review the exact session ID, Git revision, plan digest, Jakarta resource list,
complete staged topology, `/32`, expected duration, monthly-budget headroom,
cost estimate, proposed ceiling, and unconditional-teardown commands. Record
the approved ceiling only after that review:

```shell
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
```

The saved foundation plan does not yet contain image-dependent task
definitions and services. The proposal must also enumerate and cost the final
staged topology that `aws-async-deploy` creates.

Obtain explicit authorization for this exact session, plan, ceiling, and
unconditional teardown. Any commit after planning invalidates the saved plan;
create and review a new session instead of attempting to repair it.

## 4. Apply the approved foundation

```shell
make aws-up \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  AWS_MONTHLY_BUDGET_USD=25
```

This applies only the saved, approved foundation plan. RDS is normally the
longest quiet step. If apply fails, use manual teardown below. If it succeeds,
continue directly to deployment without manually modifying AWS.

## 5. Publish, migrate, and converge the services

```shell
make aws-async-deploy \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

The deployment controller performs these steps internally:

1. Build and publish the API, worker, and simulator as Linux AMD64 images.
2. Resolve and journal their immutable digests.
3. Save, hash, and apply the exact runtime plan with services disabled.
4. Run the one-off migration task and require a zero exit status.
5. Save, hash, and apply the exact service plan.
6. Require exactly two stable API tasks, one worker, and one simulator with the
   frozen CPU and memory reservations.

A normal failure, `Ctrl-C`, or `SIGTERM` triggers destroy and native
verification. Successful convergence intentionally leaves the stack running.
Start the integration command immediately. If the process or machine is lost
after deployment succeeds but before integration starts, use manual teardown.
If migration fails, inspect `ecs-migration-result.json` in the session evidence
for its bounded, non-secret ECS stop code and container exit code.

## 6. Run integration, evidence collection, and teardown

```shell
make aws-async-integration \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID"
```

For each total-event case, the runner registers an exact synthetic manifest,
sends its requests sequentially, completes the test run, and samples exact
database/outbox state plus approximate SQS/DLQ attributes every ten seconds.
A case passes only when:

- ingestion p95 is below 500 ms and errors are below 1%;
- every accepted event reconciles through RDS, outbox, queue, worker, and the
  private simulator;
- no duplicate business effect or incorrect final shipment exists;
- the DLQ remains empty and work first drains no later than 120 seconds; and
- the empty state remains continuously observed for 180 seconds.

The three stable-drain windows alone require at least nine minutes. After all
cases pass, the controller also requires the seven-widget dashboard and
published ALB, API, simulator, worker, SQS, and RDS CloudWatch series. Success,
failure, `Ctrl-C`, and `SIGTERM` all proceed to complete destroy and native
absence verification.

## 7. Verify the terminal result

```shell
uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

root = Path("results/aws-sessions") / os.environ["TRACKRELAY_RUN_SESSION_ID"]
manifest = json.loads((root / "session.json").read_text())
summary = json.loads((root / "async-integration" / "summary.json").read_text())
native = json.loads(
    (root / "aws-native-inventory-after-destroy.json").read_text()
)

print("session status:", manifest["status"])
print("all guardrails passed:", summary["all_guardrails_passed"])
for point in summary["points"]:
    processing = point["processing"]
    print(
        "point:",
        point["event_count"],
        "total events; passed:",
        point["guardrails_passed"],
        "p95 ms:",
        point["ingestion_p95_ms"],
        "errors %:",
        point["ingestion_error_percent"],
        "drained at s:",
        processing["drain_reached_after_seconds"],
        "confirmed at s:",
        processing["drain_confirmed_after_seconds"],
    )
print("CloudWatch series:", [s["query_id"] for s in summary["cloudwatch"]["series"]])
print("nonzero native inventory:", {k: v for k, v in native.items() if v})
PY
```

Required final state:

- `session status: teardown_verified`
- `all guardrails passed: True`
- Exactly the `1`, `10`, and `100` total-event cases, all passed
- Six published CloudWatch series
- Empty Terraform state
- No nonzero native inventory count

Private raw evidence is retained under:

```text
results/aws-sessions/<session ID>/async-integration/
```

## Manual teardown and recovery

Use this if foundation apply fails, a successful deployment is not immediately
followed by integration, or the local controller disappears before cleanup.

```shell
make aws-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"

make aws-verify-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  AWS_DEPLOYMENT_MODE=async \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

If destroy fails, still run verification, preserve both errors, diagnose the
specific remaining resource, and retry the complete destroy. Targeted destroy
is not the normal recovery path. Teardown is complete only when Terraform state
is empty and native inventory is zero.

## Do not

- Interpret `1`, `10`, or `100` as events/s or report a capacity result from
  this checkpoint.
- Substitute the Stage 9.3 rate ladder or start the Stage 9.6 workload in cloud
  session 3.
- Reuse a prior session ID, changed plan, or different Git revision.
- Broaden ingress beyond the approved `/32`.
- Manually change tasks, services, queues, RDS, or dashboard resources.
- Run another lifecycle command while a controller owns the stack.
- Leave the successful deployment alive for interactive analysis.

See also:

- `docs/aws-async-architecture.md`
- `docs/aws-cloud-session-checklist.md`
- `docs/aws-cost-controls.md`
- `docs/implementation-plan.md`
