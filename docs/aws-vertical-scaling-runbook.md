# Stage 9.3 AWS hardware-flexibility runbook

This procedure runs one short RDS-backed comparison:

```text
t3.small -> c7i-flex.large
```

The application, image digest, API process count, database pool, RDS instance,
downstream simulator, load driver, and pass rules stay fixed. Each machine gets
`10, 25, 50, 100, 200` events/s for ten seconds per point and stops at its first
failure. The result asks only whether `c7i-flex.large` passes a higher offered
rate. It is a short cloud hardware-flexibility demonstration, not an autoscaling
or long-duration capacity claim.

k6 initializes one VU per offered event/s before each timed point. Thus the
largest point initializes 200 VUs, and k6 never grows the VU pool during the
measurement.

## Operator workflow

The normal workflow has only three commands:

```text
make aws-plan
      ↓ explicit approval
make aws-up
      ↓
make aws-scaling-session
```

Do not run `aws-rehost-deploy`, `aws-rehost-workload`, `aws-rds-deploy`, or
`aws-rds-correctness` between these commands. Those remain useful historical
Stage 9.1/9.2 tools, but their workload changes the T3 CPU-credit starting
condition and is not Stage 9.3 preparation.

`aws-up` is kept separate as the explicit spending boundary. After
`aws-scaling-session` validates its approval, that process owns image
publication, direct RDS deployment, correctness, clean-state preparation,
measurement, reporting, teardown, and native absence verification. Do not run a
second controller in parallel.

## 1. Create a fresh session identity

Run from the repository root with a clean worktree. Generate the UTC identity
and derive the public `/32` of the machine that will run k6:

```shell
export TRACKRELAY_RUN_SESSION_ID="cloud-session-2-$(date -u +%Y%m%dT%H%M%SZ)"

trackrelay_public_ipv4="$(
  curl --fail --silent --show-error https://checkip.amazonaws.com
)"
trackrelay_public_ipv4="$(
  uv run --locked python -c \
    'import ipaddress, sys; print(ipaddress.IPv4Address(sys.argv[1]))' \
    "$trackrelay_public_ipv4"
)"
export TRACKRELAY_RUN_API_CIDR="${trackrelay_public_ipv4}/32"
export TRACKRELAY_RUN_TIER_ORDER='t3.small,c7i-flex.large'

test ! -e "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID"
printf 'session: %s\ningress: %s\ntiers: %s\n' \
  "$TRACKRELAY_RUN_SESSION_ID" \
  "$TRACKRELAY_RUN_API_CIDR" \
  "$TRACKRELAY_RUN_TIER_ORDER"
```

Do not choose a cost ceiling until the saved plan and estimated session cost
have been reviewed.

If an approved plan already exists, recover its values instead of generating a
new identity:

```shell
export TRACKRELAY_RUN_SESSION_FILE='results/aws-sessions/APPROVED_SESSION_ID/session.json'
export TRACKRELAY_RUN_SESSION_ID="$(
  jq -er '.session_id' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_API_CIDR="$(
  jq -er '.api_ingress_cidr' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
export TRACKRELAY_RUN_TIER_ORDER='t3.small,c7i-flex.large'
```

For an existing plan, verify its status, revision, and digest:

```shell
test "$(jq -er '.status' "$TRACKRELAY_RUN_SESSION_FILE")" = 'planned'
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

## 2. Run local preflight

```shell
git status --short
git rev-parse HEAD
make aws-check
make infra-init
make infra-check
make test
```

Required checkpoints:

- Git status is empty.
- The AWS identity is `trackrelay-admin` in `ap-southeast-3`.
- Terraform formatting, validation, and tests pass.
- The Python test suite passes.

The Makefile passes the profile and region explicitly; no Codex-specific or
ambient AWS configuration is required.

## 3. Save and review the plan

Skip this section only when executing an existing approved plan.

```shell
make aws-plan \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Inspect the saved plan:

```shell
terraform -chdir=infra/terraform show \
  "../../results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/terraform.tfplan"
```

Review the exact session ID, Git revision, plan digest, Jakarta resource list,
`t3.small,c7i-flex.large` order, estimated duration, cost ceiling, and
unconditional teardown. Record the approved ceiling only after that review:

```shell
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
```

Any commit after planning invalidates the saved plan. Create a new session and
plan rather than attempting to repair it.

## 4. Apply the approved infrastructure

```shell
make aws-up \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

This applies the saved plan, which creates the disposable EC2 host, private RDS
database, ECR repository, and networking. RDS is normally the longest quiet
step. If apply fails, use manual teardown below. If it succeeds, proceed
directly to the armed session—do not run a preliminary workload.

## 5. Run the complete failure-safe session

```shell
make aws-scaling-session \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  APPROVED_TIER_ORDER="$TRACKRELAY_RUN_TIER_ORDER" \
  APPROVED_UNCONDITIONAL_TEARDOWN_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

The runner performs these steps internally:

1. Publish the approved Git revision as a digest-pinned Linux AMD64 image.
2. Install TrackRelay directly against private RDS on the fresh EC2 host. It
   does not deploy or benchmark host-local PostgreSQL.
3. Run all four RDS correctness scenarios.
4. Reset the generated database rows and downstream receipts, read them back as
   empty, and save reset evidence.
5. Verify that T3 is in Standard credit mode and wait for a recent CloudWatch
   `CPUCreditBalance` datapoint of at least `1.0` credit.
6. Freeze the experiment definition and run the short T3 ladder, resetting
   after every point and stopping at its first failure.
7. Reset again, apply and validate the in-place move to `c7i-flex.large`, then
   run the identical ladder from 10/s.
8. Generate the compact comparison report.
9. Destroy the entire stack and verify Terraform, tagged, and native AWS
   inventories.

T3 Standard receives no launch credits. It earns 24 credits/hour while running,
and AWS publishes `CPUCreditBalance` at five-minute resolution. Consequently,
the credit gate can be a quiet several-minute wait. It is intentional: a
missing, stale, depleted, or Unlimited-mode reading aborts before traffic rather
than producing another ambiguous baseline.

At a first failing rate, k6 can print `level=error` for `checks` or
`dropped_iterations`. That is measured overload when the enclosing runner
continues through reconciliation. `AWS vertical-scaling session failed` means
the workflow itself failed and should be diagnosed from the saved session
evidence.

The session attempts destroy and native verification after publication,
deployment, correctness, measurement, reporting, ordinary failures, `Ctrl-C`,
and `SIGTERM`. `SIGKILL`, destruction of the controller machine, or loss of
Terraform state cannot be caught; use manual recovery in those cases.

## 6. Verify the terminal result

```shell
uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

root = Path("results/aws-sessions") / os.environ["TRACKRELAY_RUN_SESSION_ID"]
manifest = json.loads((root / "session.json").read_text())
print("session status:", manifest["status"])
print("report:", manifest.get("vertical_scaling", {}).get("report"))
native = json.loads(
    (root / "aws-native-inventory-after-destroy.json").read_text()
)
print("nonzero native inventory:", {k: v for k, v in native.items() if v})
PY
```

Required final state:

- `session status: teardown_verified`
- A saved Stage 9.3 report
- Empty Terraform state
- No nonzero native inventory count

Private raw evidence is retained under:

```text
results/aws-sessions/<session ID>/vertical-scaling/
```

## Manual teardown and recovery

Use this if apply fails, the armed runner cannot start, or its local process is
lost. Read the journaled instance type and pending transition from
`session.json`; do not guess after a resize.

```shell
make aws-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"

make aws-verify-down \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=JOURNALED_INSTANCE_TYPE \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

If destroy fails, still run verification, preserve both errors, diagnose the
specific remaining resource, and retry the complete destroy. Targeted destroy
is not the normal recovery path.

## Do not

- Reuse a prior session ID or saved plan.
- Run from a dirty or different Git revision.
- Broaden ingress beyond the approved `/32`.
- Run any host-local portability workload before Stage 9.3.
- Resize RDS, alter process/pool settings, or change downstream capacity.
- Run another lifecycle command while the armed session owns the stack.
- Present the short result as autoscaling or a long-duration capacity limit.
- Leave the stack alive for interactive analysis after the session.

See also:

- `docs/aws-cloud-session-checklist.md`
- `docs/aws-vertical-scaling-experiment.md`
- `docs/aws-cost-controls.md`
