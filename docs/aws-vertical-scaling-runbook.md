# Stage 9.3 AWS hardware-flexibility runbook

This is the operator-facing procedure for running TrackRelay's complete
RDS-backed synchronous hardware-flexibility experiment. It is intentionally
separate from the implementation details in
`docs/aws-vertical-scaling-experiment.md`.

The experiment uses the same ordered six-rate candidate ladder on:

```text
t3.small -> c7i-flex.large -> m7i-flex.large
```

All three types are x86_64 and expose two vCPUs. Only the EC2 instance type changes. The application revision and image, one API
process, connection pool, private RDS database, downstream simulator, load
driver, rates, duration, and pass/fail rules remain fixed.

## Important ownership rule

Before `aws-scaling-session` starts, you are responsible for running setup or
teardown commands. Once `aws-scaling-session` starts, it becomes the only
controller: do not run another scaling, transition, report, destroy, or verify
command in parallel.

The armed runner attempts destroy and native verification after success,
ordinary failure, `Ctrl-C`, or `SIGTERM`. It cannot recover from destruction of
the local machine, loss of Terraform state, or `SIGKILL`; keep the workspace and
session evidence on durable storage.

## 1. Set the session inputs

Run from the repository root with a clean worktree. There are two distinct
modes. Do not generate a new session ID when executing a plan that already
exists.

### Mode A — create a fresh proposal

Generate the UTC session timestamp instead of handwriting it. Obtain the public
IPv4 from the same machine that will run k6, validate it with Python's standard
library, and convert it to the single-address CIDR accepted by Terraform:

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

export TRACKRELAY_RUN_TIER_ORDER='t3.small,c7i-flex.large,m7i-flex.large'
test ! -e "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID"
printf 'session: %s\ningress: %s\ntiers: %s\n' \
  "$TRACKRELAY_RUN_SESSION_ID" \
  "$TRACKRELAY_RUN_API_CIDR" \
  "$TRACKRELAY_RUN_TIER_ORDER"
```

The `test ! -e` guard prevents accidental session reuse. Do not set the cost
ceiling yet: it is a human authorization decision made after the saved plan and
cost estimate have been reviewed.

Continue through the preflight below and run `make aws-plan` in section 2. That
command creates `session.json` and `terraform.tfplan`. The human-readable
`proposal.md` is then written beside them during plan and cost review; it is not
created by Terraform itself.

### Mode B — execute an approved plan that already exists

Select the exact approved session record deliberately. Derive its session ID
and CIDR from that record rather than regenerating either value. The cost
ceiling must be copied from the explicit approval because treating a file as
authority for its own spending limit would make the approval check circular.

Replace `APPROVED_SESSION_ID` once with the exact ID from the approval:

```shell
export TRACKRELAY_RUN_SESSION_FILE='results/aws-sessions/APPROVED_SESSION_ID/session.json'

export TRACKRELAY_RUN_SESSION_ID="$(
  jq -er '.session_id' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_API_CIDR="$(
  jq -er '.api_ingress_cidr' "$TRACKRELAY_RUN_SESSION_FILE"
)"
export TRACKRELAY_RUN_COST_CEILING_USD='REVIEWED_APPROVAL_CEILING'
export TRACKRELAY_RUN_TIER_ORDER='t3.small,c7i-flex.large,m7i-flex.large'
```

Verify that the selected plan is still the approved, unmodified plan:

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

printf 'session: %s\ningress: %s\nceiling: USD %s\ntiers: %s\nplan SHA-256: %s\n' \
  "$TRACKRELAY_RUN_SESSION_ID" \
  "$TRACKRELAY_RUN_API_CIDR" \
  "$TRACKRELAY_RUN_COST_CEILING_USD" \
  "$TRACKRELAY_RUN_TIER_ORDER" \
  "$trackrelay_actual_plan_sha"
```

These task-specific variables deliberately avoid ambient `AWS_PROFILE` and
Terraform variables. The Makefile supplies the project profile
`trackrelay-admin` and region `ap-southeast-3` explicitly.

Confirm the revision and toolchain:

```shell
git status --short
git rev-parse HEAD
make aws-check
make infra-init
make infra-check
make test
```

Confirm that the account deliberately remains on the Free plan and that its
credit balance is visible before proposing resources:

```shell
aws \
  --profile trackrelay-admin \
  --region us-east-1 \
  freetier get-account-plan-state \
  --query '{plan:accountPlanType,status:accountPlanStatus,remainingCredits:accountPlanRemainingCredits}' \
  --output table
```

This revised ladder avoids the paid-plan-only C8g types. Regional
`DescribeInstanceTypes` output alone is not sufficient: architecture must also
match across every in-place transition, which is why the run begins on x86_64
`t3.small` instead of ARM64 `t4g.small`.

Expected checkpoints:

- Git status is empty.
- AWS preflight reports `trackrelay-admin`, Jakarta, and no resource changes.
- Terraform validation and all Terraform tests pass.
- The ordinary test suite passes.

If you are in Mode A, continue to section 2. If you are in Mode B, the approved
plan already exists: **skip section 2 and continue directly to section 3**.

## 2. Create—but do not apply—the plan (Mode A only)

```shell
make aws-plan \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

This contacts read-only AWS data sources and writes a speculative saved plan; it
does not create resources. Inspect:

```shell
terraform -chdir=infra/terraform show \
  "../../results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/terraform.tfplan"

sed -n '1,240p' \
  "results/aws-sessions/$TRACKRELAY_RUN_SESSION_ID/proposal.md"
```

Do not apply until the exact session ID, plan digest, resource list, frozen tier
order, cost ceiling, and unconditional teardown have explicit approval. Any Git
commit after planning invalidates the plan automatically; create a new session
and plan instead of trying to repair the old one.

## 3. Apply only the approved saved plan

Record the UTC start time and start the approved duration timer. Then run:

```shell
make aws-up \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  APPROVED_COST_CEILING_USD="$TRACKRELAY_RUN_COST_CEILING_USD" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Notes:

- The command applies the saved plan file, not a newly generated plan.
- RDS creation is normally the longest quiet provisioning step.
- Detailed provider output is saved privately under the session directory.
- If this or any following setup command fails before the armed runner starts,
  immediately use the manual teardown procedure below.

## 4. Publish and smoke-test the synchronous deployment

Publish the exact committed Linux AMD64 image:

```shell
make aws-rehost-publish \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Deploy the digest-pinned image through SSM and run the small on-host smoke test:

```shell
make aws-rehost-deploy \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Expected checkpoints:

- The session manifest contains a Linux AMD64 image digest.
- The deployment uses that digest, not a mutable tag.
- Migrations, API readiness, downstream readiness, ingestion, and persistence
  pass without SSH access.

## 5. Preserve the portability checkpoint and switch to RDS

Run the frozen short host-local portability workload required by the normal
Stage 9.3 state machine:

```shell
make aws-rehost-workload \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

This is setup evidence, not the RDS-backed Stage 9.3 baseline. It runs six short
rate points and saves each reconciliation result under `rehost-workload/`.
At rates above the small host's capacity, k6 may print `level=error` because the
`dropped_iterations` threshold was crossed. That is an expected measured
overload result. The checkpoint itself passes only when the enclosing
`make aws-rehost-workload` command finishes successfully and advances the
session; an `AWS rehost command failed` message is not an expected k6 result.

Switch the same deployment to the already provisioned private RDS instance:

```shell
make aws-rds-deploy \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Do not use `aws-rds-deploy-canary` here. That shortcut exists only for a
proposal explicitly limited to the non-publishable sampler canary.

Run the four RDS correctness scenarios:

```shell
make aws-rds-correctness \
  SESSION_ID="$TRACKRELAY_RUN_SESSION_ID" \
  REHOST_INSTANCE_TYPE=t3.small \
  API_INGRESS_CIDR="$TRACKRELAY_RUN_API_CIDR"
```

Do not continue unless all four scenarios pass and the session status is
`rds_correctness_collected`.

## 6. Hand ownership to the failure-safe experiment runner

This is the long-running experiment command:

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

The runner performs, in order:

1. Freeze the exact image, RDS, workload, hardware, and guardrail controls.
2. Run 180-second candidates on `t3.small`, stopping after the first fully
   collected failed point.
3. Preserve evidence, reset synthetic state, and validate the in-place move to
   `c7i-flex.large`.
4. Replay the candidate ladder from its lowest rate on `c7i-flex.large`, again
   stopping after the first failure.
5. Preserve evidence, reset state, and validate the in-place move to
   `m7i-flex.large`.
6. Replay the candidate ladder from its lowest rate on `m7i-flex.large`, stopping
   after the first failure.
7. Generate the two-transition comparison and bottleneck report.
8. Destroy the complete Terraform stack.
9. Verify empty Terraform state, the generic tag inventory, and all native
   service inventories.

For every rate, it requires:

- Exact k6 process boundaries and the complete k6 summary.
- A detached private sampler ready before load begins.
- A completed controller stop handshake immediately after k6 exits, followed
  by one final process-observation attempt; the duration-plus-150-second timeout is
  only an orphan-safety bound.
- Timestamped API and downstream observation attempts containing the whole
  load window. Individual overload-time gaps are retained explicitly instead
  of crashing the observer; diagnostic calculations use successful snapshots
  and remain fail-closed when those snapshots are insufficient.
- Sanitized driver-side runtime-observation gaps. A strict pre-load sample must
  pass, but a one-second metrics timeout during or after overload cannot discard
  the completed k6 result or prevent reconciliation.
- A bounded post-load SSM recovery probe before reconciliation. Collection is
  retried only after `Undeliverable` or `DeliveryTimedOut` with response code
  -1; any command that began execution is never replayed. All command attempts
  and terminal delivery statuses remain in the per-rate evidence directory.
- Proof that the run-specific sampler container was removed.
- Complete database and downstream reconciliation.
- EC2 and RDS CloudWatch evidence aligned to the load window.
- A valid pass/fail interpretation; failed overload is never counted as useful
  throughput merely because it consumed CPU.

Long quiet periods are expected while CloudWatch metrics publish, EC2 changes
state, SSM comes back online, or RDS is being destroyed. Do not start a second
controller during those waits.

## 7. Verify the terminal result

After the command returns, inspect compact, non-secret evidence:

```shell
uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

root = Path("results/aws-sessions") / os.environ["TRACKRELAY_RUN_SESSION_ID"]
manifest = json.loads((root / "session.json").read_text())
print("session status:", manifest["status"])
print(
    "report:",
    manifest.get("vertical_scaling", {}).get("report"),
)
native = json.loads(
    (root / "aws-native-inventory-after-destroy.json").read_text()
)
print("nonzero native inventory:", {k: v for k, v in native.items() if v})
PY
```

Required terminal state:

- `session status: teardown_verified`
- A saved Stage 9.3 comparison report
- Empty Terraform state
- No nonzero native inventory count

Raw experiment evidence lives under:

```text
results/aws-sessions/<session ID>/vertical-scaling/
```

This directory is intentionally ignored by Git because it can contain private
operational evidence. The final reviewed, portable result should be copied into
the repository only through a separate, deliberate reporting step.

## Manual teardown and recovery

Use this immediately if setup fails before `aws-scaling-session` starts, or if
the runner is lost because the local process or machine disappears:

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

Read `rehost_instance_type` and any pending transition target from the session's
`session.json`; do not guess the cleanup tier. If destroy fails, still run
verification, preserve both errors, diagnose the exact remaining resource, and
retry complete destroy. Do not use targeted destroy as the normal recovery.

## What not to do

- Do not reuse a prior session ID or saved plan.
- Do not run with a dirty worktree or a different Git revision.
- Do not broaden API ingress beyond the approved `/32`.
- Do not use ambient Codex-specific environment configuration.
- Do not resize RDS, change worker counts, tune the pool, or alter the downstream
  limit during the experiment.
- Do not interpret host-local portability evidence as the RDS-backed baseline.
- Do not turn a diagnostic canary into a publishable rate point.
- Do not leave the stack alive for interactive analysis after the runner ends.

See also:

- `docs/aws-cloud-session-checklist.md`
- `docs/aws-vertical-scaling-experiment.md`
- `docs/aws-cost-controls.md`
